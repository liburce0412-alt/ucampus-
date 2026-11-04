package local.ucampus.audio;

import android.content.Context;
import android.content.pm.PackageManager;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecordingConfiguration;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.util.Base64;
import org.json.JSONObject;
import java.io.File;
import java.io.IOException;
import java.lang.reflect.InvocationTargetException;
import java.nio.file.Files;
import java.util.concurrent.atomic.AtomicBoolean;

/** One UID, one short PCM clip, no speaker output or persistent settings. */
public final class InjectionProbe {
    private static final String TARGET = "cn.unipus.cloud";
    private static final String PCM = "/data/local/tmp/ucampus-audio-input.pcm";
    private static final int RATE = 16000;

    private static void event(String phase, Object... pairs) throws Exception {
        JSONObject row = new JSONObject().put("phase", phase);
        for (int i = 0; i < pairs.length; i += 2) row.put((String)pairs[i], pairs[i + 1]);
        System.out.println(row.toString());
        System.out.flush();
    }

    private static Object call(Object target, String method, Class<?>[] types, Object... args) throws Exception {
        return target.getClass().getMethod(method, types).invoke(target, args);
    }

    private static void play(AudioTrack track, byte[] pcm) throws Exception {
        long deadline = SystemClock.elapsedRealtime() + 22000;
        int offset = 0;
        track.play();
        while (offset < pcm.length && SystemClock.elapsedRealtime() < deadline) {
            int count = track.write(pcm, offset, Math.min(3200, pcm.length - offset), AudioTrack.WRITE_NON_BLOCKING);
            if (count < 0) throw new IOException("AudioTrack write failed: " + count);
            offset += count;
            if (count == 0) SystemClock.sleep(5);
        }
        while (track.getPlaybackHeadPosition() < pcm.length / 2 && SystemClock.elapsedRealtime() < deadline) SystemClock.sleep(10);
        if (offset != pcm.length || track.getPlaybackHeadPosition() < pcm.length / 2) throw new IOException("PCM delivery timed out");
        event("played", "bytes", offset, "frames", track.getPlaybackHeadPosition());
    }

    private static void capture(AudioRecord recorder, int uid, int user, int seconds) throws Exception {
        AtomicBoolean stop = new AtomicBoolean();
        Thread input = new Thread(() -> {
            try { System.in.read(); } catch (IOException ignored) { }
            stop.set(true);
        }, "capture-stop");
        input.setDaemon(true);
        input.start();
        recorder.startRecording();
        if (recorder.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) throw new IOException("Capture did not start");
        event("ready", "uid", uid, "user", user, "pid", Process.myPid(), "mode", "capture", "rate", RATE, "channels", 1);
        byte[] buffer = new byte[3200];
        long deadline = SystemClock.elapsedRealtime() + seconds * 1000L;
        int total = 0;
        while (!stop.get() && SystemClock.elapsedRealtime() < deadline) {
            int count = recorder.read(buffer, 0, buffer.length, AudioRecord.READ_NON_BLOCKING);
            if (count < 0 || count % 2 != 0) throw new IOException("Invalid capture read: " + count);
            if (count == 0) { SystemClock.sleep(5); continue; }
            total += count;
            if (total > RATE * 2 * seconds + buffer.length) throw new IOException("Capture exceeded its sample limit");
            event("pcm", "data", Base64.encodeToString(buffer, 0, count, Base64.NO_WRAP));
            if (System.out.checkError()) throw new IOException("Capture consumer disconnected");
        }
        recorder.stop();
        event("captured", "bytes", total, "stopped", stop.get());
    }

    public static void main(String[] args) throws Exception {
        boolean capturing = args.length == 3 && args[0].equals("capture");
        if (!capturing && (args.length != 2 || !(args[0].equals("check") || args[0].equals("inject"))))
            throw new IllegalArgumentException("check|inject exact_uid; capture exact_uid seconds");
        final int seconds = capturing ? Integer.parseInt(args[2]) : 0;
        if (capturing && (seconds < 1 || seconds > 900)) throw new IllegalArgumentException("Capture duration must be 1..900 seconds");
        final int uid = Integer.parseInt(args[1]);
        if (uid < 10000 || Process.myUid() != 2000) throw new SecurityException("Use the authorized shell and an application UID");
        final boolean inject = args[0].equals("inject");
        byte[] pcm = null;
        if (inject) {
            File input = new File(PCM);
            if (!input.getCanonicalPath().equals(PCM) || input.length() < 2 || input.length() > RATE * 2 * 20 || input.length() % 2 != 0)
                throw new IllegalArgumentException("Expected a bounded mono PCM16 clip at the exact test path");
            pcm = Files.readAllBytes(input.toPath());
        }
        Thread watchdog = new Thread(() -> {
            SystemClock.sleep(capturing ? (seconds + 15) * 1000L : 45000);
            System.err.println("Audio probe exceeded its deadline; binder death must release its policy");
            Process.killProcess(Process.myPid());
        }, "injection-deadline");
        watchdog.setDaemon(true);
        watchdog.start();

        Object policy = null;
        AudioTrack track = null;
        AudioRecord recorder = null;
        AudioManager manager = null;
        Class<?> policyType = null;
        boolean registered = false;
        int exit = 1;
        String stage = "context";
        try {
            Looper.prepareMainLooper();
            // Reuse the installed scrcpy runtime's Android 17 shell context setup.
            Class.forName("com.genymobile.scrcpy.Workarounds").getMethod("apply").invoke(null);
            Context context = (Context)Class.forName("com.genymobile.scrcpy.FakeContext").getMethod("get").invoke(null);
            int user = (Integer)Class.forName("android.app.ActivityManager").getMethod("getCurrentUser").invoke(null);
            String[] packages = context.getPackageManager().getPackagesForUid(uid);
            if (uid / 100000 != user || packages == null || packages.length != 1 || !TARGET.equals(packages[0]))
                throw new SecurityException("UID does not belong exclusively to U-campus in the active user");
            if (context.checkSelfPermission("android.permission.MODIFY_AUDIO_ROUTING") != PackageManager.PERMISSION_GRANTED)
                throw new SecurityException("Shell has no existing audio routing grant");
            manager = (AudioManager)context.getSystemService(Context.AUDIO_SERVICE);
            for (AudioRecordingConfiguration recording : manager.getActiveRecordingConfigurations()) {
                int owner = (Integer)AudioRecordingConfiguration.class.getMethod("getClientUid").invoke(recording);
                if (owner == uid) throw new IllegalStateException("U-campus is already recording; wait for a safe boundary");
            }
            stage = "build-policy";
            Class<?> ruleType = Class.forName("android.media.audiopolicy.AudioMixingRule");
            Object ruleBuilder = Class.forName("android.media.audiopolicy.AudioMixingRule$Builder").getConstructor().newInstance();
            call(ruleBuilder, "setTargetMixRole", new Class<?>[]{int.class}, ruleType.getField(capturing ? "MIX_ROLE_PLAYERS" : "MIX_ROLE_INJECTOR").getInt(null));
            call(ruleBuilder, "addMixRule", new Class<?>[]{int.class, Object.class}, ruleType.getField("RULE_MATCH_UID").getInt(null), uid);
            Object rule = call(ruleBuilder, "build", new Class<?>[]{});
            Class<?> mixType = Class.forName("android.media.audiopolicy.AudioMix");
            Object mixBuilder = Class.forName("android.media.audiopolicy.AudioMix$Builder").getConstructor(ruleType).newInstance(rule);
            AudioFormat format = new AudioFormat.Builder().setSampleRate(RATE).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).setEncoding(AudioFormat.ENCODING_PCM_16BIT).build();
            call(mixBuilder, "setFormat", new Class<?>[]{AudioFormat.class}, format);
            call(mixBuilder, "setRouteFlags", new Class<?>[]{int.class}, mixType.getField("ROUTE_FLAG_LOOP_BACK").getInt(null));
            Object mix = call(mixBuilder, "build", new Class<?>[]{});
            policyType = Class.forName("android.media.audiopolicy.AudioPolicy");
            Object builder = Class.forName("android.media.audiopolicy.AudioPolicy$Builder").getConstructor(Context.class).newInstance(context);
            call(builder, "addMix", new Class<?>[]{mixType}, mix);
            policy = call(builder, "build", new Class<?>[]{});
            stage = "register";
            int status = (Integer)AudioManager.class.getMethod("registerAudioPolicy", policyType).invoke(manager, policy);
            if (status != 0) throw new IllegalStateException("AudioPolicy registration rejected: " + status);
            registered = true;
            if (capturing) {
                stage = "create-recorder";
                recorder = (AudioRecord)policyType.getMethod("createAudioRecordSink", mixType).invoke(policy, mix);
                if (recorder == null || recorder.getState() != AudioRecord.STATE_INITIALIZED) throw new IOException("Capture AudioRecord is not initialized");
                stage = "capture";
                capture(recorder, uid, user, seconds);
            } else {
                stage = "create-track";
                track = (AudioTrack)policyType.getMethod("createAudioTrackSource", mixType).invoke(policy, mix);
                if (track == null || track.getState() != AudioTrack.STATE_INITIALIZED) throw new IllegalStateException("Injection AudioTrack is not initialized");
                event("ready", "uid", uid, "user", user, "pid", Process.myPid(), "mode", args[0]);
            }
            if (inject) {
                stage = "wait-play";
                if (System.in.read() != 'P') throw new IOException("Missing explicit play command");
                stage = "play";
                play(track, pcm);
                stage = "wait-stop";
                if (System.in.read() != 'X') throw new IOException("Missing release command");
            }
            exit = 0;
        } catch (Throwable error) {
            if (error instanceof InvocationTargetException && error.getCause() != null) error = error.getCause();
            event("error", "stage", stage, "type", error.getClass().getName(), "message", String.valueOf(error.getMessage()));
        } finally {
            if (registered) try {
                AudioManager.class.getMethod("unregisterAudioPolicy", policyType).invoke(manager, policy);
                event("released", "policy_status", policyType.getMethod("getStatus").invoke(policy));
            } catch (Throwable error) {
                exit = 2;
                event("release-error", "message", error.toString());
            }
            if (track != null) track.release();
            if (recorder != null) recorder.release();
        }
        System.exit(exit);
    }
}

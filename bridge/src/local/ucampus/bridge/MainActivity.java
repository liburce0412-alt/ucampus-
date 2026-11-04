package local.ucampus.bridge;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.provider.Settings;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

public final class MainActivity extends Activity {
    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(32, 64, 32, 32);
        TextView text = new TextView(this);
        text.setText("U校园控制测试\n\n仅在你允许测试时启用无障碍服务。电脑经 ADB 读取当前控件、等待变化和点击。应用不联网，不录音，不读取其他应用内容。\n\n开启后回到 U校园即可。测试结束可在这里停止。");
        layout.addView(text);
        Button enable = new Button(this);
        enable.setText("打开无障碍设置");
        enable.setOnClickListener(v -> startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)));
        layout.addView(enable);
        Button stop = new Button(this);
        stop.setText("停止本工具");
        stop.setOnClickListener(v -> {
            BridgeService service = BridgeService.current;
            if (service != null) service.disableSelf();
            finish();
        });
        layout.addView(stop);
        setContentView(layout);
    }
}

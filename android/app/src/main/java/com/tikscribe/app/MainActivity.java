package com.tikscribe.app;

import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        handleSendIntent();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleSendIntent();
    }

    private void handleSendIntent() {
        Intent intent = getIntent();
        if (intent == null || !Intent.ACTION_SEND.equals(intent.getAction())) return;
        if (!"text/plain".equals(intent.getType())) return;

        String sharedText = intent.getStringExtra(Intent.EXTRA_TEXT);
        if (sharedText == null || sharedText.isEmpty()) return;

        // Escape for JS injection
        String escaped = sharedText
            .replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace("\n", "\\n")
            .replace("\r", "");

        // Inject shared text into WebView — retry at staggered delays
        // to handle page load timing (just like a text message app pastes a link)
        String js = "window._sharedIntentText = '" + escaped + "';"
                  + "var el = document.getElementById('url-input');"
                  + "if(el){ el.value = '" + escaped + "'; }";

        Handler handler = new Handler(Looper.getMainLooper());
        int[] delays = {500, 1000, 2000, 3000};
        for (int delay : delays) {
            handler.postDelayed(() -> {
                try {
                    getBridge().getWebView().evaluateJavascript(js, null);
                } catch (Exception e) {
                    // WebView not ready yet, next retry will handle it
                }
            }, delay);
        }
    }
}

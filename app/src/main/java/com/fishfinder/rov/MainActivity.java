package com.fishfinder.rov;

import android.app.DownloadManager;
import android.content.Context;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.webkit.ConsoleMessage;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.URLUtil;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;

public class MainActivity extends AppCompatActivity {

    private WebView webView;
    private static final String LOCAL_URL = "file:///android_asset/www/index.html";
    private ConnectivityManager connectivityManager;
    private ConnectivityManager.NetworkCallback wifiNetworkCallback;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        try {
            // Keep screen awake while piloting ROV
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            setContentView(R.layout.activity_main);

            // Force app traffic onto local ROV Wi-Fi router (e.g. COFE_AF9B)
            // even when Android says "Connected to device. Can't provide Internet."
            bindToWifiNetwork();

            webView = findViewById(R.id.webview);
            configureWebView();
            webView.loadUrl(LOCAL_URL);
        } catch (Exception e) {
            Toast.makeText(this, "Init Error: " + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    /**
     * Binds this app process directly to the active Wi-Fi transport network,
     * bypassing Android's cellular fallback when the ROV router has no internet.
     */
    private void bindToWifiNetwork() {
        try {
            connectivityManager = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
            if (connectivityManager == null) return;

            // First check all existing networks for an active Wi-Fi transport
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                for (Network net : connectivityManager.getAllNetworks()) {
                    NetworkCapabilities caps = connectivityManager.getNetworkCapabilities(net);
                    if (caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) {
                        connectivityManager.bindProcessToNetwork(net);
                        break;
                    }
                }
            }

            // Also register a callback so if Wi-Fi connects/reconnects to COFE_AF9B, we bind immediately
            NetworkRequest.Builder builder = new NetworkRequest.Builder()
                    .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                    .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);

            wifiNetworkCallback = new ConnectivityManager.NetworkCallback() {
                @Override
                public void onAvailable(Network network) {
                    try {
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                            connectivityManager.bindProcessToNetwork(network);
                        } else {
                            ConnectivityManager.setProcessDefaultNetwork(network);
                        }
                    } catch (Exception ignored) {}
                }

                @Override
                public void onLost(Network network) {
                    try {
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                            connectivityManager.bindProcessToNetwork(null);
                        }
                    } catch (Exception ignored) {}
                }
            };

            connectivityManager.requestNetwork(builder.build(), wifiNetworkCallback);
        } catch (Exception ignored) {}
    }

    private void configureWebView() {
        if (webView == null) return;

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setAllowFileAccess(true);
        settings.setAllowContentAccess(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);

        // Hardware accelerated canvas & video rendering
        webView.setLayerType(View.LAYER_TYPE_HARDWARE, null);
        webView.setScrollBarStyle(View.SCROLLBARS_INSIDE_OVERLAY);

        // Native JS Bridge
        webView.addJavascriptInterface(new WebAppInterface(this), "AndroidBridge");

        // WebChromeClient for console/dialog handling
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onConsoleMessage(ConsoleMessage cm) {
                return super.onConsoleMessage(cm);
            }
        });

        // WebViewClient to keep navigation in-app
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                view.loadUrl(url);
                return true;
            }
        });

        // Handle File Downloads (Photos & Videos)
        webView.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent, String contentDisposition, String mimeType, long contentLength) {
                try {
                    DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
                    request.setMimeType(mimeType);
                    String fileName = URLUtil.guessFileName(url, contentDisposition, mimeType);
                    request.setTitle(fileName);
                    request.setDescription("Saving FishFinder media...");
                    request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                    request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, fileName);

                    DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
                    if (dm != null) {
                        dm.enqueue(request);
                        Toast.makeText(MainActivity.this, "Saving: " + fileName, Toast.LENGTH_SHORT).show();
                    }
                } catch (Exception e) {
                    Toast.makeText(MainActivity.this, "Download error: " + e.getMessage(), Toast.LENGTH_SHORT).show();
                }
            }
        });
    }

    private void hideSystemUI() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                final WindowInsetsController insetsController = getWindow().getInsetsController();
                if (insetsController != null) {
                    insetsController.hide(WindowInsets.Type.statusBars() | WindowInsets.Type.navigationBars());
                    insetsController.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
                }
            } else {
                getWindow().getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                    | View.SYSTEM_UI_FLAG_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                    | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                    | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                );
            }
        } catch (Exception ignored) {}
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            hideSystemUI();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        hideSystemUI();
        bindToWifiNetwork();
        if (webView != null) {
            webView.onResume();
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (webView != null) {
            webView.onPause();
        }
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        try {
            if (connectivityManager != null && wifiNetworkCallback != null) {
                connectivityManager.unregisterNetworkCallback(wifiNetworkCallback);
            }
        } catch (Exception ignored) {}
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    public class WebAppInterface {
        Context mContext;

        WebAppInterface(Context c) {
            mContext = c;
        }

        @JavascriptInterface
        public void showToast(String toast) {
            Toast.makeText(mContext, toast, Toast.LENGTH_SHORT).show();
        }

        @JavascriptInterface
        public void forceWifiRoute() {
            runOnUiThread(() -> bindToWifiNetwork());
        }
    }
}

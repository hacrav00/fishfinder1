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

import android.content.ContentValues;
import android.media.MediaScannerConnection;
import android.provider.MediaStore;
import android.util.Base64;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;

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

            if (Build.VERSION.SDK_INT <= Build.VERSION_CODES.P) {
                if (checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(new String[]{android.Manifest.permission.WRITE_EXTERNAL_STORAGE, android.Manifest.permission.READ_EXTERNAL_STORAGE}, 100);
                }
            }

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
        settings.setAllowFileAccessFromFileURLs(true);
        settings.setAllowUniversalAccessFromFileURLs(true);
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
        private File tempMediaFile = null;
        private FileOutputStream tempMediaOut = null;
        private String transferFileName = null;
        private String transferMimeType = null;

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

        @JavascriptInterface
        public void setHandOrientation(String mode) {
            runOnUiThread(() -> {
                try {
                    if ("left_cam".equalsIgnoreCase(mode)) {
                        // Left Hand at Camera Side, Right Hand at Charging Port Side
                        setRequestedOrientation(android.content.pm.ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE);
                    } else if ("right_cam".equalsIgnoreCase(mode)) {
                        // Right Hand at Camera Side, Left Hand at Charging Port Side
                        setRequestedOrientation(android.content.pm.ActivityInfo.SCREEN_ORIENTATION_REVERSE_LANDSCAPE);
                    } else {
                        // Auto-flip between both landscape grips using gravity sensor
                        setRequestedOrientation(android.content.pm.ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE);
                    }
                } catch (Exception ignored) {}
            });
        }

        @JavascriptInterface
        public void saveMediaBase64(String base64Data, String fileName, String mimeType) {
            new Thread(() -> {
                try {
                    if (base64Data == null || base64Data.isEmpty()) return;
                    String raw = base64Data;
                    int comma = raw.indexOf(",");
                    if (comma != -1) {
                        raw = raw.substring(comma + 1);
                    }
                    byte[] bytes = Base64.decode(raw, Base64.DEFAULT);
                    File temp = new File(getCacheDir(), "media_" + System.currentTimeMillis() + "_" + fileName);
                    try (FileOutputStream fos = new FileOutputStream(temp)) {
                        fos.write(bytes);
                    }
                    commitTempFileToStorage(temp, fileName, mimeType);
                } catch (Exception e) {
                    runOnUiThread(() -> Toast.makeText(mContext, "Save error: " + e.getMessage(), Toast.LENGTH_LONG).show());
                }
            }).start();
        }

        @JavascriptInterface
        public void initMediaTransfer(String fileName, String mimeType) {
            try {
                transferFileName = fileName;
                transferMimeType = mimeType;
                tempMediaFile = new File(getCacheDir(), "chunk_" + System.currentTimeMillis() + "_" + fileName);
                if (tempMediaFile.exists()) tempMediaFile.delete();
                tempMediaOut = new FileOutputStream(tempMediaFile);
            } catch (Exception e) {
                e.printStackTrace();
            }
        }

        @JavascriptInterface
        public void appendMediaChunk(String chunkBase64) {
            try {
                if (tempMediaOut != null && chunkBase64 != null && !chunkBase64.isEmpty()) {
                    byte[] chunk = Base64.decode(chunkBase64, Base64.DEFAULT);
                    tempMediaOut.write(chunk);
                }
            } catch (Exception e) {
                e.printStackTrace();
            }
        }

        @JavascriptInterface
        public void finishMediaTransfer() {
            new Thread(() -> {
                try {
                    if (tempMediaOut != null) {
                        tempMediaOut.flush();
                        tempMediaOut.close();
                        tempMediaOut = null;
                    }
                    if (tempMediaFile != null && tempMediaFile.exists()) {
                        commitTempFileToStorage(tempMediaFile, transferFileName, transferMimeType);
                    }
                } catch (Exception e) {
                    runOnUiThread(() -> Toast.makeText(mContext, "Media transfer error: " + e.getMessage(), Toast.LENGTH_LONG).show());
                }
            }).start();
        }

        private void commitTempFileToStorage(File sourceFile, String fileName, String mimeType) {
            if (sourceFile == null || !sourceFile.exists()) return;
            try {
                boolean isImage = (mimeType != null && mimeType.startsWith("image"));
                boolean isVideo = (mimeType != null && mimeType.startsWith("video"));

                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    ContentValues values = new ContentValues();
                    values.put(MediaStore.MediaColumns.DISPLAY_NAME, fileName);
                    values.put(MediaStore.MediaColumns.MIME_TYPE, mimeType);

                    Uri collectionUri;
                    if (isImage) {
                        values.put(MediaStore.Images.Media.RELATIVE_PATH, Environment.DIRECTORY_PICTURES + "/FishFinder");
                        collectionUri = MediaStore.Images.Media.EXTERNAL_CONTENT_URI;
                    } else if (isVideo) {
                        values.put(MediaStore.Video.Media.RELATIVE_PATH, Environment.DIRECTORY_MOVIES + "/FishFinder");
                        collectionUri = MediaStore.Video.Media.EXTERNAL_CONTENT_URI;
                    } else {
                        values.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/FishFinder");
                        collectionUri = MediaStore.Downloads.EXTERNAL_CONTENT_URI;
                    }
                    values.put(MediaStore.MediaColumns.IS_PENDING, 1);

                    Uri itemUri = getContentResolver().insert(collectionUri, values);
                    if (itemUri != null) {
                        try (InputStream in = new FileInputStream(sourceFile);
                             OutputStream out = getContentResolver().openOutputStream(itemUri)) {
                            byte[] buffer = new byte[65536];
                            int bytesRead;
                            while ((bytesRead = in.read(buffer)) != -1) {
                                out.write(buffer, 0, bytesRead);
                            }
                        }
                        values.clear();
                        values.put(MediaStore.MediaColumns.IS_PENDING, 0);
                        getContentResolver().update(itemUri, values, null, null);

                        runOnUiThread(() -> {
                            String typeStr = isImage ? "📸 Photo" : "🎥 Video";
                            Toast.makeText(MainActivity.this, typeStr + " saved to Gallery (" + (isImage ? "Pictures" : "Movies") + "/FishFinder)", Toast.LENGTH_LONG).show();
                        });
                    } else {
                        throw new Exception("MediaStore insertion failed");
                    }
                } else {
                    File dir = new File(Environment.getExternalStoragePublicDirectory(
                            isImage ? Environment.DIRECTORY_PICTURES : (isVideo ? Environment.DIRECTORY_MOVIES : Environment.DIRECTORY_DOWNLOADS)
                    ), "FishFinder");
                    if (!dir.exists()) dir.mkdirs();
                    File destFile = new File(dir, fileName);

                    try (InputStream in = new FileInputStream(sourceFile);
                         OutputStream out = new FileOutputStream(destFile)) {
                        byte[] buffer = new byte[65536];
                        int bytesRead;
                        while ((bytesRead = in.read(buffer)) != -1) {
                            out.write(buffer, 0, bytesRead);
                        }
                    }

                    MediaScannerConnection.scanFile(
                            MainActivity.this,
                            new String[]{destFile.getAbsolutePath()},
                            new String[]{mimeType},
                            null
                    );

                    runOnUiThread(() -> {
                        String typeStr = isImage ? "📸 Photo" : "🎥 Video";
                        Toast.makeText(MainActivity.this, typeStr + " saved to Gallery (" + destFile.getName() + ")", Toast.LENGTH_LONG).show();
                    });
                }
            } catch (Exception e) {
                runOnUiThread(() -> Toast.makeText(MainActivity.this, "Save failed: " + e.getMessage(), Toast.LENGTH_LONG).show());
            } finally {
                try {
                    if (sourceFile != null && sourceFile.exists()) {
                        sourceFile.delete();
                    }
                } catch (Exception ignored) {}
            }
        }
    }
}

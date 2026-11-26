import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "com.floodguard.ai",
  appName: "FloodGuard AI",
  webDir: "mobile/www",
  server: { androidScheme: "https" },
  android: {
    // Required for the optional same-Wi-Fi HTTP backend; UI validates private LAN addresses.
    allowMixedContent: true
  }
};

export default config;

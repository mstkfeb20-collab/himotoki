package com.example.order.client;

import java.util.Properties;

public final class Config {
    private static final Properties P = new Properties();
    static {
        try { P.load(Config.class.getResourceAsStream("/config.properties")); } catch (Exception e) { /* ignore */ }
    }
    public static String get(String key) { return P.getProperty(key); }
}

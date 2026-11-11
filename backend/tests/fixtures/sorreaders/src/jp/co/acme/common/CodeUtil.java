package jp.co.acme.common;
public final class CodeUtil {
    public static final String KEY_CD = "cd";
    public static String zeroPad(String v, int n) {
        StringBuffer sb = new StringBuffer();
        for (int i = v.length(); i < n; i++) { sb.append("0"); }
        sb.append(v);
        return sb.toString();
    }
}

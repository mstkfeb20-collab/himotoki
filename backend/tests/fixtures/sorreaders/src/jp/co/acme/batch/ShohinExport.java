package jp.co.acme.batch;
import java.io.FileWriter;
public class ShohinExport {
    public static void main(String[] args) throws Exception {
        String key = "export.shohin.file";
        FileWriter w = new FileWriter(System.getProperty(key));
        w.close();
    }
}

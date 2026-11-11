package jp.co.acme.web;
import java.util.HashMap;
import java.util.Map;
import jp.co.acme.common.CodeUtil;
public class KakakuAction extends org.apache.struts.action.Action {
    private com.ibatis.sqlmap.client.SqlMapClient sqlMapClient;
    public Object execute(Object mapping, Object form) throws Exception {
        TankaForm f = (TankaForm) form;
        Map p = new HashMap();
        p.put(CodeUtil.KEY_CD, CodeUtil.zeroPad(f.getKeyword(), 10));
        p.put("tanka", "0");
        sqlMapClient.update("Rireki.updateTanka", p);
        return null;
    }
}

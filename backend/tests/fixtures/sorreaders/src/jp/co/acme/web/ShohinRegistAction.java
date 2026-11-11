package jp.co.acme.web;
public class ShohinRegistAction extends org.apache.struts.action.Action {
    public Object execute(ShohinForm form) {
        String sql = "INSERT INTO M_SHOHIN (SHOHIN_CD, SHOHIN_NM) VALUES (?, ?)";
        return sql;
    }
}

package jp.co.acme.web;
import java.sql.CallableStatement;
import javax.servlet.http.HttpServletRequest;
public class HaishiAction extends org.apache.struts.action.Action {
    private java.sql.Connection con;
    public Object execute(Object mapping, Object form, HttpServletRequest request) throws Exception {
        CallableStatement cs = con.prepareCall("{call PKG_SHOHIN.HAISHI(?, ?)}");
        cs.setString(1, request.getParameter("haishiCd"));
        cs.setString(2, request.getParameter("riyu"));
        return cs.execute();
    }
}

package jp.co.acme.web;
import org.apache.struts.action.ActionForm;
import org.apache.struts.action.DynaActionForm;
public class ShohinKensakuAction extends org.apache.struts.action.Action {
    private java.sql.Connection con;
    public Object execute(Object mapping, ActionForm form) throws Exception {
        DynaActionForm f = (DynaActionForm) form;
        String nm = (String) f.get("shohinNm");
        java.sql.PreparedStatement ps = con.prepareStatement("SELECT * FROM M_SHOHIN WHERE SHOHIN_NM = ?");
        ps.setString(1, nm);
        return ps.executeQuery();
    }
}

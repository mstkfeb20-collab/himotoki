package jp.co.acme.dao;
import java.sql.Connection;
import java.sql.PreparedStatement;
public class ShohinDao {
    private static final String INSERT_SQL =
        "INSERT INTO M_SHOHIN (SHOHIN_CD, SHOHIN_NM, UPD_DT) VALUES (?, ?, SYSDATE)";
    private Connection con;
    public int insert(ShohinDto d) throws Exception {
        PreparedStatement ps = con.prepareStatement(INSERT_SQL);
        int i = 1;
        ps.setString(i++, d.getShohinCd());
        ps.setString(i++, d.getShohinName());
        return ps.executeUpdate();
    }
}

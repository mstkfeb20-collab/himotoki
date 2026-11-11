package jp.co.acme.web;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import javax.servlet.http.HttpServletRequest;
import org.apache.commons.beanutils.PropertyUtils;
import jp.co.acme.dao.ShohinDto;

/** 確認画面: 登録画面でセッションに置いた内容を確定する */
public class ShohinConfirmAction extends org.apache.struts.action.Action {
    private com.ibatis.sqlmap.client.SqlMapClient sqlMapClient;
    private org.springframework.jdbc.core.JdbcTemplate jdbcTemplate;

    public Object execute(Object mapping, Object form, HttpServletRequest request) throws Exception {
        ShohinDto dto = (ShohinDto) request.getSession().getAttribute("SHOHIN");
        sqlMapClient.insert("Rireki.insert", dto);
        TankaForm tf = (TankaForm) form;
        ShohinTankaBean bean = new ShohinTankaBean();
        PropertyUtils.copyProperties(bean, tf);
        Map params = new HashMap();
        params.put("tanka", bean.getTanka());
        params.put("cd", dto.getShohinCd());
        sqlMapClient.update("Rireki.updateTanka", params);
        List args = new ArrayList();
        StringBuffer sql = new StringBuffer("SELECT * FROM M_SHOHIN WHERE 1 = 1");
        if (tf.getKeyword() != null) {
            sql.append(" AND SHOHIN_NM LIKE ?");
            args.add("%" + tf.getKeyword() + "%");
        }
        jdbcTemplate.queryForList(sql.toString(), args.toArray());
        return null;
    }
}

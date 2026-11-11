package jp.co.acme.web;

import javax.servlet.http.HttpServletRequest;
import org.apache.commons.beanutils.BeanUtils;
import org.apache.struts.action.ActionForm;
import jp.co.acme.dao.ShohinDto;
import jp.co.acme.service.ShohinService;

public class ShohinRegistAction extends org.apache.struts.action.Action {
    private ShohinService service;

    public Object execute(Object mapping, ActionForm form, HttpServletRequest request) throws Exception {
        ShohinForm f = (ShohinForm) form;
        ShohinDto dto = new ShohinDto();
        dto.setShohinCd(f.getShohinCd().trim());
        String nm = f.getShohinNm();
        dto.setShohinName(nm == null ? "" : nm.toUpperCase());
        request.getSession().setAttribute("SHOHIN", dto);
        service.regist(dto);
        service.search(request.getParameter("keyword"));
        return null;
    }
}

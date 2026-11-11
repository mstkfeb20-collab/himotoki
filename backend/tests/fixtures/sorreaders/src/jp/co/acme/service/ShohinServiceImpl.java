package jp.co.acme.service;
import java.sql.Connection;
import java.sql.PreparedStatement;
import jp.co.acme.dao.ShohinDto;
import jp.co.acme.dao.ShohinDao;
import jp.co.acme.mapper.ShohinMapper;
public class ShohinServiceImpl implements ShohinService {
    private ShohinDao dao;
    private ShohinMapper mapper;
    public void regist(ShohinDto dto) {
        dao.insert(dto);
    }
    public void search(String keyword) {
        mapper.findByName(StringUtil.likeEscape(keyword));
    }
}

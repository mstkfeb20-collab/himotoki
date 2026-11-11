package jp.co.acme.service;
import jp.co.acme.dao.ShohinDto;
public interface ShohinService {
    void regist(ShohinDto dto);
    void search(String keyword);
}

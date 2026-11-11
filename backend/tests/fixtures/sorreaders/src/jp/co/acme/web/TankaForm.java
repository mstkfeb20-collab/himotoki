package jp.co.acme.web;
public class TankaForm extends org.apache.struts.action.ActionForm {
    private String tanka;
    private String keyword;
    private String memo;
    public String getTanka() { return tanka; }
    public String getKeyword() { return keyword; }
    public String getMemo() { return memo; }
}

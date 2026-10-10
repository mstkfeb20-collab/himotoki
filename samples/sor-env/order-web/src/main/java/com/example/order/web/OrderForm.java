package com.example.order.web;

import javax.validation.constraints.Size;

public class OrderForm {
    private String customerId;
    private String itemId;
    private int qty;
    /** 備考（受注メモ） */
    @Size(max = 200)
    private String note;

    public String getCustomerId() { return customerId; }
    public String getItemId() { return itemId; }
    public int getQty() { return qty; }
    public String getNote() { return note; }
}

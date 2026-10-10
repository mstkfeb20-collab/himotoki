package com.example.order.ejb;

import javax.ejb.Local;

@Local
public interface OrderService {
    String placeOrder(String customerId, String itemId, int qty, String note);
    void cancelOrder(String orderId);
}

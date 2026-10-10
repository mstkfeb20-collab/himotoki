package com.example.order.web;

import javax.ejb.EJB;
import javax.ws.rs.POST;
import javax.ws.rs.Path;
import javax.ws.rs.PathParam;
import com.example.order.ejb.OrderService;

/** 受注受付API（画面・外部システムから呼ばれる） */
@Path("/orders")
public class OrderResource {

    @EJB
    private OrderService orderService;

    @POST
    public String placeOrder(OrderForm form) {
        return orderService.placeOrder(form.getCustomerId(), form.getItemId(), form.getQty(), form.getNote());
    }

    @POST
    @Path("/{orderId}/cancel")
    public void cancel(@PathParam("orderId") String orderId) {
        orderService.cancelOrder(orderId);
    }
}

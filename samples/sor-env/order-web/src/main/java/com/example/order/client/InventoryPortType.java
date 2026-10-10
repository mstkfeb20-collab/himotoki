package com.example.order.client;

import javax.jws.WebMethod;
import javax.jws.WebParam;
import javax.jws.WebService;

/** 在庫サービスのポート（wsimport で生成したインタフェースを想定） */
@WebService(name = "InventoryPortType", targetNamespace = "http://example.com/inventory")
public interface InventoryPortType {

    @WebMethod(operationName = "reserveStock")
    String reserveStock(@WebParam(name = "itemId") String itemId, @WebParam(name = "qty") int qty,
                        @WebParam(name = "orderId") String orderId);

    @WebMethod(operationName = "releaseStock")
    void releaseStock(@WebParam(name = "orderId") String orderId);
}

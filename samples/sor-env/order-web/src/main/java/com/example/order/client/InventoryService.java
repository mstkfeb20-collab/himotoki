package com.example.order.client;

import javax.xml.ws.Service;
import javax.xml.ws.WebServiceClient;

@WebServiceClient(name = "InventoryService", targetNamespace = "http://example.com/inventory",
        wsdlLocation = "META-INF/wsdl/inventory.wsdl")
public class InventoryService extends Service {
    public InventoryService(java.net.URL wsdl) {
        super(wsdl, new javax.xml.namespace.QName("http://example.com/inventory", "InventoryService"));
    }

    public InventoryPortType getInventoryPort() {
        return getPort(InventoryPortType.class);
    }
}

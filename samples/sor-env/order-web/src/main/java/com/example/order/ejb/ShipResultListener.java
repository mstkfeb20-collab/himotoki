package com.example.order.ejb;

import javax.ejb.ActivationConfigProperty;
import javax.ejb.MessageDriven;
import javax.inject.Inject;
import javax.jms.Message;
import javax.jms.MessageListener;
import javax.jms.TextMessage;
import com.example.order.dao.OrderDao;

/** 出荷サーバから届く出荷結果（JMS）を受けて受注の出荷状況を更新する。 */
@MessageDriven(mappedName = "jms/ShipResultQueue", activationConfig = {
        @ActivationConfigProperty(propertyName = "destinationType", propertyValue = "javax.jms.Queue")})
public class ShipResultListener implements MessageListener {

    @Inject
    private OrderDao orderDao;

    @Override
    public void onMessage(Message message) {
        try {
            String[] f = ((TextMessage) message).getText().split(",");
            orderDao.updateShipStatus(f[0], f[1]);
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }
}

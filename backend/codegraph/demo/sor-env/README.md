# 検証用のSoR構成（自作サンプル）

複数サーバにまたがる業務と、トランザクション・連携の典型的な書き方を確かめるために作ったサンプルです（実在のシステムではありません）。3つのリポジトリ（受注・在庫・出荷）を1つのシステムとして解析します。

| リポジトリ | 内容 |
|---|---|
| `order-web` | 受注API（JAX-RS）、EJB（コンテナ管理 REQUIRED / REQUIRES_NEW、Bean管理で100件ごとコミット、MDB）、JDBC直書きのSQL、ストアド呼び出し、在庫SOAPクライアント、出荷RESTクライアント、ejb-jar.xml・weblogic-ejb-jar.xml・WebLogic JDBC（XA）、Oracleのパッケージ（COMMIT・自律型トランザクション・FOR UPDATE・ループ内コミット）、JP1・sqlplus・sftp送信・ftp受信・SQL*Loader |
| `inventory-svc` | 在庫SOAPサービス（@WebService、WSDL）、受注DBへのDBリンク越しの更新（PKG_STOCK） |
| `shipping-svc` | 出荷REST API、受注CSVの取り込み（JP1ファイル監視ジョブの後続）、出荷結果のJMS送信と結果ファイル出力 |

業務の流れ（抜粋）:

1. 受注API → OrderServiceBean.placeOrder（REQUIRED）→ ORDERS・ORDER_ITEMS 登録 → 在庫SOAP reserveStock（在庫サーバで STOCK 更新、PKG_STOCK が DBリンクで受注DBの ORDER_STOCK_HIST に書き込み）→ 監査ログ（REQUIRES_NEW）→ PKG_ORDER.CONFIRM_ORDER（内部で COMMIT）→ 出荷REST /shipments
2. 夜間 J100（sqlplus で ORDERS を orders.csv に出力）→ J110（sftp で出荷サーバの /recv/orders.csv へ）→ 出荷サーバ J200（ファイル監視）→ J210（ImportOrdersMain が SHIPMENT_REQ に取り込み）
3. 出荷結果: 出荷サーバが JMS で受注サーバの ShipResultListener へ送信、ファイル /send/ship_result.csv にも出力 → 受注サーバ J120（ftp get）→ J130（SQL*Loader で ORDER_SHIP_RESULT へ）

`codegraph demo` がこのサンプルをワークスペースの `repos/` に置き、インデックスしてビュワーを開きます。手で行う場合は次のとおりです。

```bash
codegraph index --system sor --repo order-web --repo inventory-svc --repo shipping-svc   # このフォルダで実行
codegraph analyze --graph sor "受注の備考（NOTE）の上限を200文字から500文字に拡張する"
```

期待される結果（`backend/tests/test_sor_system.py` で確認しています）:

- サーバ間の呼び出しがつながる: SOAPクライアントのインタフェース → 在庫サーバの `@WebService` 実装（名前空間＋オペレーション名で照合）、RESTのパス `/shipments` → 出荷サーバのエンドポイント、`@Resource(lookup="jms/ShipResultQueue")` → MDB、DBリンク越しの書き込み、CSVの受け渡し（spool → cp → sftp put → ファイル監視 → 取り込み）
- `index` の結果に `transactions` が出る。`placeOrder` は REQUIRED の境界で、トランザクション内のSOAP/REST呼び出し、REQUIRES_NEW の監査ログ、ストアド内COMMIT、自律型トランザクションが注意点として付く。`cancelOrder` は ejb-jar.xml の指定で REQUIRES_NEW
- NOTE 拡張の分析で、受注DB・画面項目だけでなく、出力SQL → orders_work.csv → orders.csv → 送信シェル → ファイル監視ジョブ → 取り込みジョブ → ImportOrdersMain → SHIPMENT_REQ.NOTE までが候補に入る

-- 連携ファイル: 当日予約一覧（予約番号, ツアー, 顧客, 人数, 備考）
SELECT r.reserve_no, r.tour_code, c.customer_code, c.customer_name, c.customer_mail,
       r.adult_count, r.child_count, r.remarks
  FROM reserve r
  JOIN customer c ON c.customer_code = r.customer_code
 WHERE r.reserved_day = CURRENT_DATE;

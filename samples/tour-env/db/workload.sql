-- 検証用シナリオ: アプリ相当のSQLと、コードに無いSQL（夜間帳票の動的SQL）を流して
-- pg_stat_statements に実行時の記録を残す。

-- アプリ（MyBatis mapper）と同じ形のSQL
SELECT reserve_no, tour_code, reserved_day, adult_count, child_count, customer_code, transfer, sum_price, remarks
  FROM reserve WHERE reserve_no = '00000001';
SELECT customer_code, customer_name, customer_kana, customer_pass, customer_birth, customer_job, customer_mail,
       customer_tel, customer_post, customer_add FROM customer WHERE customer_code = '00000001';
SELECT age_code, age_name, age_rate FROM age WHERE age_code = '0';
UPDATE reserve SET adult_count = adult_count, child_count = child_count, transfer = transfer, sum_price = sum_price,
       remarks = remarks WHERE reserve_no = '00000001';
SELECT COUNT(*) FROM reserve WHERE tour_code = '0000000001';

-- コードのどこにも無いSQL（運用スクリプトや帳票ツールから発行される想定）
SELECT c.customer_name, c.customer_rank, COUNT(*) AS cnt, SUM(r.sum_price) AS total
  FROM reserve r JOIN customer c ON c.customer_code = r.customer_code
 GROUP BY c.customer_name, c.customer_rank ORDER BY total DESC;
SELECT * FROM v_reserve_summary WHERE tour_code = '0000000001';

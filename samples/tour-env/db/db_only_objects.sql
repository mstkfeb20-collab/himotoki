-- 検証用シナリオ: 本番DBにだけ存在し、リポジトリのDDLには無いオブジェクト
-- （SoRでよくある「DBに直接当てた改修」「ストアドに埋まった業務ロジック」を再現する）

-- 1) DBに直接追加された列（DDLとのずれ検出の対象）
ALTER TABLE customer ADD COLUMN customer_rank CHAR(1);
COMMENT ON COLUMN customer.customer_rank IS '顧客ランク（障害対応で直接追加）';

-- 2) 帳票用ビュー
CREATE OR REPLACE VIEW v_reserve_summary AS
SELECT r.reserve_no, r.tour_code, t.tour_name, c.customer_name, r.adult_count, r.child_count, r.sum_price
FROM reserve r
JOIN tourinfo t ON t.tour_code = r.tour_code
JOIN customer c ON c.customer_code = r.customer_code;

-- 3) 予約人数の変更で在庫（tourinfo.ava_rec_max）を減らすストアド
CREATE OR REPLACE FUNCTION fn_adjust_availability() RETURNS trigger AS $$
BEGIN
  UPDATE tourinfo SET ava_rec_max = ava_rec_max - (NEW.adult_count + NEW.child_count)
   WHERE tour_code = NEW.tour_code;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_reserve_availability AFTER INSERT ON reserve
  FOR EACH ROW EXECUTE FUNCTION fn_adjust_availability();

WITH tight AS (
  SELECT a.wallet AS w1, b.wallet AS w2, a.token
  FROM prerun_accumulators a
  JOIN prerun_accumulators b
    ON a.token=b.token AND a.wallet < b.wallet
   AND a.first_buy IS NOT NULL AND b.first_buy IS NOT NULL
   AND abs(extract(epoch from (a.first_buy - b.first_buy))) <= 900
)
SELECT w1 || chr(124) || w2 || chr(124) || COUNT(DISTINCT token)
FROM tight GROUP BY w1, w2 HAVING COUNT(DISTINCT token) >= 3;

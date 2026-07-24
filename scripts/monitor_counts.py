# -*- coding: utf-8 -*-
"""パイプライン異常検知: tenders.csv のソース別件数を前回ベースラインと比較し、
急減（サイト障害での全消し等）を検知したら非�0終了してコミット/デプロイを止める。

背景: 2026-07-24 に奈良DENCHOが0件取得→ingestが既存784件を削除し、手動確認まで
気づけなかった。各scraperに空ガードを入れたが、想定外の全消しへの二重の防御として
本スクリプトをCIのコミット前に実行する。

判定: ベースライン件数>=THRESH_MIN のソースが現在 baseline*DROP_RATIO 未満に急減
      → 異常（exit 1）。新規/少数ソースや自然増減は許容。
異常が無ければベースライン(dataset/source_counts.json)を現在値に更新して exit 0。
異常時はベースラインを更新しない（次回も検知し続ける＝復旧するまでアラート継続）。

使い方: python scripts/monitor_counts.py            # 検査（CIのコミット前）
        python scripts/monitor_counts.py --update   # 強制的にベースライン更新(誤検知時の解除)
"""
import sys, io, os, csv, json, collections

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, errors="replace", encoding="utf-8")
csv.field_size_limit(10 ** 7)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV = os.path.join(ROOT, "dataset", "tenders.csv")
BASELINE = os.path.join(ROOT, "dataset", "source_counts.json")

THRESH_MIN = 20     # これ未満のソースは急減判定しない（小規模は自然変動が大きい）
DROP_RATIO = 0.5    # baseline*0.5 未満に減ったら異常（＝50%超の急減）
TOTAL_DROP_RATIO = 0.85  # 総件数が前回の85%未満なら異常（全体的な取りこぼし）


def counts():
    rows = list(csv.DictReader(open(CSV, encoding="utf-8-sig")))
    c = collections.Counter(r.get("source", "") for r in rows)
    return dict(c), len(rows)


def main():
    force_update = "--update" in sys.argv
    cur, total = counts()
    if not os.path.exists(BASELINE):
        json.dump({"sources": cur, "total": total}, open(BASELINE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("[INIT] ベースライン作成: %d件 / %dソース" % (total, len(cur)))
        return 0
    base = json.load(open(BASELINE, encoding="utf-8"))
    bsrc, btotal = base.get("sources", {}), base.get("total", 0)

    alerts = []
    for s, bc in bsrc.items():
        if bc < THRESH_MIN:
            continue
        cc = cur.get(s, 0)
        if cc < bc * DROP_RATIO:
            alerts.append("  ⚠ %s: %d → %d (%.0f%%減)" % (s, bc, cc, (1 - cc / bc) * 100))
    if btotal and total < btotal * TOTAL_DROP_RATIO:
        alerts.append("  ⚠ 総件数: %d → %d (%.0f%%減)" % (btotal, total, (1 - total / btotal) * 100))

    if alerts and not force_update:
        print("[異常検知] ソース件数が急減しました。コミット/デプロイを中止します:")
        print("\n".join(alerts))
        print("正当な減少なら `python scripts/monitor_counts.py --update` でベースライン更新後に再実行してください。")
        return 1

    json.dump({"sources": cur, "total": total}, open(BASELINE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("[OK] 件数正常: %d件 / %dソース（ベースライン更新）" % (total, len(cur)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

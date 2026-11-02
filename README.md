# 荒川ドローンレース — コースは Z3 が設計する

*Arakawa Drone Race — the course is designed by Z3.* Built for **Built with Claude: Japan 2026** (Nov 2–10, 2026).

足立区を流れる荒川の上で（架空の）ドローンレースを開くとしたら、どんなコースなら安全で、ルールに合っているか。
条件を論理式で書き、**Z3 に条件を満たすコースと開催日を探させ**、2D の地図の上で機体を飛ばして見せます。
条件を厳しくすると「そんなコースは存在しない」と証明され（unsat）、どの条件同士がぶつかったかを示します。

**誰の、どの作業を解決するか：** 川の上でドローンのイベントを開きたい主催者が、申請の前に
「ルールを全部満たすコースと日程が存在するか」を確かめる作業。

> 架空の大会で、実際の開催提案ではありません。法令の条件は単純化した仮説のルールです。

## 仕組み

1. **川の区間**：OpenStreetMap から荒川の水面と橋を取り、橋と橋の間で一番長い区間を選ぶ。
2. **川の座標**：区間を「中心線に沿った距離 `s`」と「中心線からの横のずれ `d`」で表す。
   「ゲートが水の上にある」が `|d| ≤ 川幅/2` という線形の不等式になり、Z3 で扱いやすい。
3. **条件（Z3）**：

   | # | 条件 |
   |---|---|
   | C1 | コースは水面からはみ出さない |
   | C2 | 岸（観客エリア）から一定の距離をとる（地図のスライダーで 30〜100 m） |
   | C3 | 橋から 30 m 以上離れる |
   | C4 | 高度は 150 m 未満（レースは 5〜30 m） |
   | C5 | ゲートの間隔は 80〜250 m |
   | C6 | 左右に振る（中心線から 15 m 以上、交互） |
   | C7 | 既存の飛行計画（国交省の公開データ、足立区 1,672 件）と時間・場所が重ならない週末を選ぶ |
   | C8 | コース全体の長さは 600〜1,800 m |

   コースがあるか（sat / unsat）を確かめ、ないときは最小の衝突集合（unsat core）を返す。
   あるときは「一番早い週末 → 一番長いコース → 一番大きく振るコース」の順に最適化する。
4. **地図**：`docs/index.html` は 1 ファイルで完結（Leaflet を同梱、背景は地理院タイル）。

## 使い方

```bash
pip install z3-solver shapely pytest
python src/fetch_osm.py      # 荒川の水面と橋を OpenStreetMap から取得 → data/osm_arakawa.json
python src/build.py          # 解いて docs/index.html を書き出す
python -m pytest -q tests    # テスト（合成した直線の川で検証）
```

区間を指定するとき：`python src/build.py --pick 西新井橋 千住新橋`（OSM の橋の名前）。

## 状況（2026-11-02）

- できている：Z3 のモデル（C1–C8、unsat core）、川の区間の抽出、地図ページ（アニメーション、スライダー）、テスト 5 件。
- 未完了：**荒川の実データでの実行**。開発環境から OpenStreetMap（Overpass API）に届かず、まだ `data/osm_arakawa.json` がない。
  テストは合成した直線の川でのみ確認済み。

## データと出典

- 無人航空機飛行計画データ：国土交通省 Project LINKS／[G空間情報センター](https://www.geospatial.jp/ckan/dataset/links-mujinkoukuukihikoukeikaku-2025_)（公共データ利用規約 第1.0版）。足立区発着の 1,672 件を抽出（`data/`）。
- 川と橋：© OpenStreetMap contributors（ODbL 1.0）
- 背景地図：[地理院タイル](https://maps.gsi.go.jp/development/ichiran.html)
- 同梱ライブラリ：Leaflet 1.9.4（BSD-2-Clause、`vendor/`）

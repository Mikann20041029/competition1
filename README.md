# Weekly Opportunity Scanner (DeepSeek)

週1で「公募/コンテスト/キャンペーン候補」を集めて、DeepSeekで要件を構造化し、
提出フォームURL付きのMarkdown一覧を `output/weekly_cards.md` に生成します。

## 必要なSecrets
- DEEPSEEK_API_KEY

## 使い方
1. `config.json` の `sources` に、公募/募集が掲載されるページURLを追加
2. Actions を実行（手動なら Actions → weekly-opportunity-scan → Run workflow）
3. `output/weekly_cards.md` を開く
4. 「Submission URL」を開いて、あなたが手動で提出

## 重要
- 本ツールは提出フォーム送信はしません（規約/CAPTCHA回避のため）。
- 取得したページからDeepSeekが要件抽出しますが、誤抽出はあり得るので最終確認は必須。

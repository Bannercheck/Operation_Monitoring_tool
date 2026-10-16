# Prompts

Geliştirme sırasında kullanılan kritik prompt'lar. Tam adım listesi (araç + sürüm, tarih, iş) `docs/AI_LOG.md`'de; burada uygulamayı şekillendiren ana prompt'lar sade dille tutulur.

```
prompts/
├── README.md
├── 01-calisma-kurallari-ve-iskelet.md
├── 02-loader-ve-format-detector.md
├── 03-parser-ve-normalizasyon.md
├── 04-gurultu-azaltma-ve-incident-motoru.md
├── 05-streamlit-arayuz.md
├── 06-kanit-ve-incident-flashcard.md
├── 07-canli-operasyon-ve-ajan.md
├── 08-itsm-mcp-playbook.md
├── 09-kapsam-ve-kart-detaylari.md
├── act_select_v1.txt     ← çalışma zamanı: aksiyon ajanının yerel model seçicisinin sistem prompt'u (src/watchover/act/select.py)
├── act_explain_v1.txt    ← çalışma zamanı: istenince üretilen kanıta dayalı açıklama
├── act_next_v1.txt       ← çalışma zamanı: runbook karar noktasında seçenek önerisi
└── act_draft_v1.txt      ← çalışma zamanı: tekrar eden elle işlerden runbook taslağı (WP15)
```

`act_*_v1.txt` dosyaları geliştirme prompt'u değil, uygulamanın kendi yerel modele gönderdiği sistem metinleridir; aynı metin
`src/watchover/act/select.py` içinde de durur (imajda `prompts/` yoktur) ve `tests/test_act_select.py` ikisinin aynı kaldığını denetler.

Şablon:

| Alan | Değer |
|------|-------|
| Amaç | Ne istendi |
| Model | claude-fable-5-1 (Claude Cowork) |
| Tarih | Kullanıldığı gün |
| Sonuç | Üretilen çıktı özeti |

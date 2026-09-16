# 08 — ITSM, Connection Settings, MCP ve Playbook

| Alan | Değer |
|------|-------|
| Amaç | ITSM, Connection Settings, MCP ve Playbook |
| Model | claude-fable-5-1 (Claude Cowork) |
| Tarih | 2026-09-15 |

## Prompt

```text
ServiceNow, Jira, OneDesk ve genel bir REST ucu için ticket çekme bağlantısı yaz;
bağlantı yoksa demo ticket'lar olsun. Ticket'ları canlı sinyaller, metrik eşik
aşımları ve olaylarla ilişkilendir; ilişkiyi okunur etiketlerle ve "neden" cümlesiyle
göster, iç ID kullanma. Ayarlar sol menüde ITSM sayfasında; ana sayfada en ilgili
5 ticket görünsün.

Üç sekmeli bir ayar sayfası: HTTP API'den veri çekme, MCP sunucusuna bağlanma ve
araçlarını listeleme, yerel LLM bağlantısı (OpenLLM, Ollama gibi OpenAI uyumlu uç).
Uygulamanın motorunu dışarıdan kullanabilmek için bir MCP sunucusu yaz; Dockerfile
ve docker-compose ile dashboard ve MCP tek komutla ayağa kalksın.

Görülen her hata kalıbı bir kütüphaneye yazılsın (Playbook): kalıp, seviye, kaç kez,
hangi veri setlerinde, ekip notu. Yeni bir olayda "bunu daha önce gördük mü" diye
benzerlik araması yapsın ve notu flashcard'da göstersin.

Bitince testleri çalıştır, README'de bu adımın bölümünü güncelle, docs/AI_LOG.md'ye
araç + sürüm, tarih ve yapılan iş satırını ekle, commit at.
```

## Çıktı Özeti

itsm.py (4 sistem + demo, correlate), connectors.py (fetch_http, stdlib MCP istemcisi), llm.py, mcp_server.py (8 araç), Dockerfile + docker-compose, playbook.py (SQLite, bulanık eşleşme) ve Playbook sayfası.

## Notlar

Adım adım kayıt ve düzeltmeler için `docs/AI_LOG.md`; her dosyanın ne işe yaradığı `README.md` dosya referansında.

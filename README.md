# Claude Communication

Başka repolarda çalışan Claude Code oturumlarının, sen bilgisayar başında değilken
sana **Telegram**'dan (veya yedek olarak mail ile) yazıp cevabını beklemesi için küçük
bir araç. Sadece Python standart kütüphanesi kullanır, kurulacak paket yok.

## Kurulum (bir kere, ~3 dakika)

1. Telefonda Telegram'da **@BotFather**'a yaz → `/newbot` → bota bir isim ve sonu `bot`
   ile biten bir kullanıcı adı ver (ör. `mert_claude_bot`). Sana bir **token** verir.
2. `.env.example` dosyasını `.env` olarak kopyala, `TELEGRAM_BOT_TOKEN=` satırına
   token'ı yapıştır.
3. Çalıştır:
   ```
   python claude_comm.py setup
   ```
   Komut beklerken Telegram'da kendi botunu aç ve `/start` yaz. Sohbet kimliğin
   otomatik `.env`'e yazılır.
4. Test et:
   ```
   python claude_comm.py test --send
   ```
   Telefona butonlu bir mesaj gelir, bir butona bas, terminalde cevabın görünür.

Token'ı kimseyle paylaşma; token'a sahip olan bot adına mesaj atabilir.

## Diğer repolarda kullanmak

Aşağıda `<REPO_YOLU>`, bu reponun bilgisayardaki tam yolu, ileri eğik çizgiyle
(ör. `C:/Users/ad/Projects/Claude_Communication`).

Claude'a şunu söylemen yeterli:

> Takılırsan veya iş bitince bana yaz:
> `<REPO_YOLU>/AGENT_GUIDE.md`

Her seferinde söylemek istemezsen aynı satırı `~/.claude/CLAUDE.md` dosyasına ekle,
bütün oturumlar otomatik bilir.

**Önemli:** Sen yokken Claude'un bu komutu izin sormadan çalıştırabilmesi lazım, yoksa
onay ekranında takılı kalır. `~/.claude/settings.json` içine şunu ekle:

```json
{
  "permissions": {
    "allow": [
      "Bash(python <REPO_YOLU>/claude_comm.py:*)",
      "PowerShell(python <REPO_YOLU>/claude_comm.py:*)"
    ]
  }
}
```

Ayrıca bilgisayar **uyku moduna girmemeli**; Claude oturumu bu bilgisayarda çalışıyor.

## Telegram'da nasıl cevaplarsın

- ❓ ile başlayan mesajlar cevap bekler, ℹ️ olanlar sadece bilgidir.
- Seçenek varsa **butona bas**, bitti.
- Yazıyla cevap: **düz yazarsan** en son soru soran oturuma gider. Aynı anda birden
  fazla oturum bekliyorsa, ilgili mesaja **yanıtla (reply)** ki doğru oturuma gitsin.
- Mesajın alınınca 👍 tepkisi görürsün.
- Dosya veya fotoğraf da gönderebilirsin, Claude'a dosya yolu olarak iletilir.
- `/status` → şu an cevap bekleyen oturumları listeler.
- Sadece senin hesabından gelen mesajlar kabul edilir; başkası bota yazsa da Claude'a
  ulaşmaz.

## Komutlar

```
python claude_comm.py send  --subject "..." --body "..."           # bildirim, beklemez
python claude_comm.py ask   --subject "..." --body-file - \
                            --choice "Evet" --choice "Hayır"       # gönder ve cevap bekle
python claude_comm.py wait  --thread CC1a2b3c4d                    # beklemeye devam et
python claude_comm.py ask   --thread CC1a2b3c4d --body "..."       # aynı konuşmada devam
python claude_comm.py setup                                        # Telegram bağlantısı
python claude_comm.py test  [--send]                               # ayarları kontrol et
```

Çıkış kodları: `0` cevap geldi, `2` zaman aşımı, `1` hata.

## Mail ile kullanmak (alternatif)

`.env`'de `TELEGRAM_BOT_TOKEN` yoksa (veya `CLAUDE_COMM_BACKEND=email`) mail kullanılır.
`CLAUDE_MAIL_*` ayarları için `.env.example`'a bak. Gmail için hesapta 2 Adımlı
Doğrulama açıp bir uygulama şifresi oluşturman gerekir.

#!/usr/bin/env bash
# Watchover @VERSION@ · tek dosyalık kurulum (@KIND_LABEL@)
#
#   sudo bash @NAME@              sihirbaz: ön kontrol, ayarlar, kurulum, sonuç (ekran yoksa yazıyla sorar)
#   sudo bash @NAME@ --yes        soru sormadan, varsayılanlarla kurar
#
# Seçenekler: --check (yalnız ön kontrol, hiçbir şey değişmez)  --text (pencere yerine yazı)  --host AD (erişim adı)  --no-llm
# --no-updater  --no-host-agent  --workdir KLASÖR (açılacak yer; varsayılan /var/tmp)  --keep (açılan dosyaları silme)  --extract KLASÖR (yalnız aç)
#
# Bu dosya bir kabuk betiği ve arkasına eklenmiş bir arşivdir: betik arşivi doğrular (sha256), açar ve içindeki install-rhel.sh'yi
# çalıştırır. "@KIND@" = online: içinde kaynak kod; imajlar sunucuda derlenir, internet (ya da şirket aynası) gerekir. "offline":
# içinde imajlar, modeller, Compose ve Podman RPM'leri de var; internet gerekmez. Kurulum /opt/watchover'a yapılır; açılan
# geçici dosyalar sonunda silinir.
set -uo pipefail
WO_VERSION="@VERSION@"; WO_KIND="@KIND@"; WO_ARCH="@ARCH@"; WO_OS="@OS@"; WO_SHA256="@SHA256@"; WO_COMPRESS="@COMPRESS@"
WO_OFFSET=@OFFSET@; WO_SIZE=@SIZE@
export LANG="${LANG:-C.UTF-8}"; case "$LANG" in *[Uu][Tt][Ff]*) ;; *) export LANG=C.UTF-8 ;; esac

SELF=$(readlink -f "${BASH_SOURCE[0]:-$0}")
YES=0; TEXT=0; CHECK=0; KEEP=0; HOST=""; NO_LLM=0; NO_UPDATER=0; NO_AGENT=0; WORKBASE="${WATCHOVER_SETUP_WORKDIR:-/var/tmp}"; EXTRACT=""
ARGS=("$@")
while [ $# -gt 0 ]; do case "$1" in
  --yes|-y) YES=1 ;; --text) TEXT=1 ;; --check) CHECK=1 ;; --keep) KEEP=1 ;; --no-llm) NO_LLM=1 ;; --no-updater) NO_UPDATER=1 ;; --no-host-agent) NO_AGENT=1 ;;
  --host) HOST="${2:?--host AD}"; shift ;; --workdir) WORKBASE="${2:?--workdir KLASÖR}"; shift ;; --extract) EXTRACT="${2:?--extract KLASÖR}"; shift ;;
  -h|--help) sed -n '2,/^set -uo/p' "$SELF" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
  *) echo "Bilinmeyen seçenek: $1   (yardım: bash $(basename "$SELF") --help)" >&2; exit 2 ;;
esac; shift; done

c_b=$'\033[1m'; c_g=$'\033[32m'; c_r=$'\033[31m'; c_y=$'\033[33m'; c_0=$'\033[0m'
say() { printf '%s▸ %s%s\n' "$c_b" "$*" "$c_0"; }
fail() { printf '\n%s✗ %s%s\n' "$c_r" "$*" "$c_0" >&2; exit 1; }
strip() { sed 's/\x1b\[[0-9;]*[A-Za-z]//g'; }
free_kb() { df -Pk "$1" 2>/dev/null | awk 'NR == 2 {print $4}'; }
human() { awk -v b="$1" 'BEGIN { s = "B KB MB GB TB"; split(s, u, " "); i = 1; while (b >= 1024 && i < 5) { b /= 1024; i++ } printf (i > 2 ? "%.1f %s" : "%d %s"), b, u[i] }'; }

# ------------------------------------------------------------------------------------------------ root (sudo when not)
if [ -z "$EXTRACT" ] && [ "$(id -u)" != 0 ]; then
  command -v sudo >/dev/null 2>&1 || fail "Kurulum root olarak çalışmalı: su -c 'bash $SELF'"
  exec sudo bash "$SELF" "${ARGS[@]}"
fi

# ------------------------------------------------------------------------------------------------ the archive: check and open
payload() { tail -c +"$((WO_OFFSET + 1))" "$SELF" | head -c "$WO_SIZE"; }   # the numbers are space-padded, never octal
total=$(stat -c %s "$SELF" 2>/dev/null || wc -c < "$SELF")
[ "$total" -ge $((WO_OFFSET + WO_SIZE)) ] || fail "Kurulum dosyası eksik: $(human "$total") (olması gereken $(human $((WO_OFFSET + WO_SIZE)))). Dosyayı yeniden kopyalayın"
untar() { if [ "$WO_COMPRESS" = gzip ]; then tar xzf - -C "$1"; else tar xf - -C "$1"; fi; }
open_into() {   # DIR: one pass reads the archive, checks its sha256 and unpacks it
  local d="$1" sum need have
  need=$(( WO_SIZE / 1024 * $([ "$WO_COMPRESS" = gzip ] && echo 4 || echo 1) + 1048576 )); have=$(free_kb "$d"); have=${have:-0}
  [ "$have" -ge "$need" ] || fail "$d içinde yer yok: $(human $((have * 1024))) boş, açmak için ~$(human $((need * 1024))) gerekir. Başka bir yer: --workdir KLASÖR"
  sum=$(payload | tee >(untar "$d") | sha256sum | cut -d' ' -f1); wait
  [ "$sum" = "$WO_SHA256" ] || { rm -rf "${d:?}"/*; fail "Kurulum dosyası bozuk ya da açılamadı (sha256 tutmuyor; disk dolu olabilir): dosyayı yeniden kopyalayın"; }
  [ -f "$d/watchover-$WO_VERSION/install-rhel.sh" ] || fail "Arşivde watchover-$WO_VERSION/install-rhel.sh yok"
}
if [ -n "$EXTRACT" ]; then
  mkdir -p "$EXTRACT"; say "Açılıyor: $EXTRACT ($(human "$WO_SIZE"))"; open_into "$EXTRACT"
  printf '%s✓%s %s/watchover-%s  (kurulum: sudo bash %s/watchover-%s/install-rhel.sh)\n' "$c_g" "$c_0" "$EXTRACT" "$WO_VERSION" "$EXTRACT" "$WO_VERSION"; exit 0
fi

# ------------------------------------------------------------------------------------------------ window or text
UI=text
if [ "$TEXT" = 0 ] && [ "$YES" = 0 ] && [ -t 0 ] && [ -t 1 ]; then
  if ! command -v whiptail >/dev/null 2>&1 && [ "$WO_KIND" = online ]; then timeout 90 dnf -y -q install newt >/dev/null 2>&1 || true; fi   # newt = whiptail
  command -v whiptail >/dev/null 2>&1 && UI=window
fi
[ "$UI" = window ] || [ "$YES" = 1 ] || [ "$CHECK" = 1 ] || [ -r /dev/tty ] || fail "Soru sorulacak bir terminal yok: soru sormadan kurmak için --yes ekleyin"
TITLE="Watchover $WO_VERSION kurulumu"
H=$(tput lines 2>/dev/null || echo 24); W=$(tput cols 2>/dev/null || echo 80); H=$((H > 30 ? 26 : H - 4)); W=$((W > 100 ? 90 : W - 6))
[ "$H" -ge 12 ] || H=12; [ "$W" -ge 60 ] || W=60
export NEWT_COLORS='root=,blue window=,lightgray title=blue,lightgray border=blue,lightgray button=white,blue actbutton=white,blue compactbutton=blue,lightgray checkbox=blue,lightgray actcheckbox=white,blue entry=black,white'
wt() { whiptail --backtitle "Watchover · operasyonel gürültü → sinyal → aksiyon" --title "$TITLE" --ok-button "Tamam" --cancel-button "Vazgeç" "$@" 3>&1 1>&2 2>&3; }
ask_yes() {   # QUESTION -> 0 yes
  [ "$YES" = 1 ] && return 0
  if [ "$UI" = window ]; then wt --yesno "$1" "$H" "$W"; return; fi
  local a; read -r -p "$(printf '%s [E/h] ' "$1")" a < /dev/tty || return 1; case "$a" in ""|[EeYy]*) return 0 ;; *) return 1 ;; esac
}
ask_text() {   # QUESTION DEFAULT -> the answer
  [ "$YES" = 1 ] && { echo "$2"; return; }
  if [ "$UI" = window ]; then wt --inputbox "$1" 12 "$W" "$2" || return 1; return; fi
  local a; read -r -p "$1 [$2] " a < /dev/tty || return 1; echo "${a:-$2}"
}
run_gauge() {   # MESSAGE LOG CMD...: CMD in the background (output to LOG), a moving bar until it ends; returns its exit code
  local msg="$1" log="$2" pid p=0 t=0; shift 2
  "$@" > "$log" 2>&1 < /dev/null & pid=$!
  { while kill -0 "$pid" 2>/dev/null; do p=$(( p < 95 ? p + 3 : 95 )); printf 'XXX\n%d\n%s (%d sn)\nXXX\n' "$p" "$msg" "$t"; sleep 1; t=$((t + 1)); done
    echo 100; } | whiptail --title "$TITLE" --gauge "$msg" 8 "$W" 0
  wait "$pid"
}
show_file() {   # TITLE FILE: a box that closes with Enter when the text fits; a longer one scrolls (newt then keeps the focus on
                # the text: Tab reaches the button, the title says so)
  if [ "$UI" != window ]; then printf '\n%s── %s ──%s\n' "$c_b" "$1" "$c_0"; cat "$2"; return; fi
  local n; n=$(awk -v w=$((W - 6)) '{   # rows after newt word-wraps the text (a long path goes to a row of its own, then is cut)
    r = 1; c = 0; k = split($0, x, / +/)
    for (i = 1; i <= k; i++) { L = length(x[i]); if (L == 0) continue
      if (c > 0 && c + 1 + L > w) { r++; c = 0 }
      c += (c > 0 ? 1 : 0) + L; while (c > w) { r++; c -= w } }
    n += r } END { print n + 0 }' "$2")
  if [ "$n" -le $((H - 7)) ]; then whiptail --title "$1" --ok-button "Tamam" --textbox "$2" "$((n + 7 > 12 ? n + 7 : 12))" "$W"
  else whiptail --title "$1 · ↑↓ kaydırır, Tab → Tamam" --ok-button "Tamam" --scrolltext --textbox "$2" "$H" "$W"; fi
}

WORK="$WORKBASE/watchover-kurulum-$WO_VERSION"
cleanup() { [ "$KEEP" = 1 ] || [ -z "${WORK:-}" ] || rm -rf "${WORK:?}"; }
LOGDIR="${WATCHOVER_TEST_ROOT:-}/var/log/watchover"; mkdir -p "$LOGDIR"; OUT="$LOGDIR/kurulum-$WO_VERSION-$(date +%Y%m%d-%H%M%S).txt"

# ------------------------------------------------------------------------------------------------ 1. welcome
kind_txt=$([ "$WO_KIND" = offline ] && echo "hepsi dahil: imajlar, modeller, Compose ve Podman paketleri içinde; internet gerekmez" || echo "internetli: imajlar sunucuda derlenir (internet ya da şirket aynası gerekir)")
os_now=$(. "${WATCHOVER_TEST_ROOT:-}/etc/os-release" 2>/dev/null; echo "${PRETTY_NAME:-?}")
arch_now="${WATCHOVER_TEST_ARCH:-$(uname -m)}"
if [ "$WO_KIND" = offline ] && [ "$WO_ARCH" != "$arch_now" ]; then fail "Bu paket $WO_ARCH için hazırlanmış, bu sunucu $arch_now. $arch_now bir makinede yeniden hazırlayın: sudo watchover setup --offline"; fi
WELCOME="Watchover $WO_VERSION bu sunucuya kurulacak.

Paket : $kind_txt$([ "$WO_KIND" = offline ] && echo " · $WO_ARCH · Podman paketleri: ${WO_OS:-yok}")
Sunucu: $os_now · $arch_now
Hedef : /opt/watchover (veriler Podman birimlerinde; yeniden kurulum ve yükseltme verileri korur)

Adımlar: dosya doğrulanır ve açılır → ön kontrol (hiçbir şey değişmez) → ayarlar → kurulum (5-20 dk) → sonuç.

Devam edilsin mi?"
if [ "$UI" = window ]; then wt --yes-button "Devam" --no-button "Vazgeç" --yesno "$WELCOME" "$H" "$W" || { echo "Vazgeçildi."; exit 0; }
else printf '\n%s%s%s\n\n%s\n' "$c_b" "$TITLE" "$c_0" "${WELCOME%Devam edilsin mi?}"; [ "$YES" = 1 ] || ask_yes "Devam edilsin mi?" || { echo "Vazgeçildi."; exit 0; }; fi

# ------------------------------------------------------------------------------------------------ 2. open the archive
mkdir -p "$WORKBASE"; rm -rf "${WORK:?}"; mkdir -p "$WORK"; chmod 700 "$WORK"
if [ "$UI" = window ]; then
  run_gauge "Dosya doğrulanıyor ve açılıyor ($(human "$WO_SIZE"))" "$WORKBASE/.watchover-open.log" open_into "$WORK"; rc=$?
  [ "$rc" = 0 ] || { wt --msgbox "$(strip < "$WORKBASE/.watchover-open.log")" 12 "$W"; rm -f "$WORKBASE/.watchover-open.log"; cleanup; exit 1; }
  rm -f "$WORKBASE/.watchover-open.log"
else
  say "Dosya doğrulanıyor ve açılıyor: $(human "$WO_SIZE") → $WORK"; ( open_into "$WORK" ) || { cleanup; exit 1; }
  printf '  %s✓%s sha256 doğru, açıldı\n' "$c_g" "$c_0"
fi
SRC="$WORK/watchover-$WO_VERSION"

# ------------------------------------------------------------------------------------------------ 3. preflight
pre=("$SRC/install-rhel.sh" --check); [ -z "$HOST" ] || pre+=(--host "$HOST"); [ "$NO_LLM" = 0 ] || pre+=(--no-llm)
if [ "$UI" = window ]; then
  run_gauge "Ön kontrol: işletim sistemi, kaynaklar, portlar, paket depoları, internet" "$WORK/check.txt" bash "${pre[@]}"; rc=$?
  { strip < "$WORK/check.txt" | sed -n '/Ön kontrol/,$p' > "$WORK/check.all"
    printf '✓ %s kontrol uygun\n' "$(grep -c '^ *✓' "$WORK/check.all")"
    grep -E '^ *(!|✗)' "$WORK/check.all" | sed 's/^ *//'
    if grep -q 'Ön kontrol tamam' "$WORK/check.all"; then echo "Kurulum başlayabilir."; else grep -E 'Kurulum başlamadı' "$WORK/check.all" | tail -1; fi
    printf '\nAyrıntı: %s\n' "${OUT%.txt}-on-kontrol.txt"; } > "$WORK/check.view"; strip < "$WORK/check.txt" > "${OUT%.txt}-on-kontrol.txt"
  if [ "$rc" != 0 ]; then show_file "Ön kontrol: kurulum başlayamaz (hiçbir şey değiştirilmedi)" "$WORK/check.view"; cleanup; exit 1; fi
  show_file "Ön kontrol tamam ✓ = uygun, ! = bilgi" "$WORK/check.view"
else
  say "Ön kontrol"; bash "${pre[@]}" 2>&1 | tee "$WORK/check.txt" | sed -n '/Ön kontrol/,$p' | tail -n +2; rc=${PIPESTATUS[0]}
  [ "$rc" = 0 ] || { cleanup; fail "Ön kontrol geçmedi (hiçbir şey değiştirilmedi): yukarıdaki ✗ satırları"; }
fi
[ "$CHECK" = 0 ] || { echo "Ön kontrol tamam (--check: hiçbir şey kurulmadı)."; cleanup; exit 0; }

# ------------------------------------------------------------------------------------------------ 4. settings
fqdn=$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo ""); cur=""
[ -f "${WATCHOVER_TEST_ROOT:-}/opt/watchover/.env" ] && cur=$(sed -n 's/^WATCHOVER_EDGE_HOST=//p' "${WATCHOVER_TEST_ROOT:-}/opt/watchover/.env" | tail -1)
HOST=${HOST:-${cur:-$fqdn}}
HOST=$(ask_text "Kullanıcıların açacağı adres (sertifikanın ilk adı). Yük dengeleyici adı varsa onu yazın, örn. watchover.sirket.com.tr:" "$HOST") || { echo "Vazgeçildi."; cleanup; exit 0; }
mem_gb=$(awk '/^MemTotal:/ {printf "%d", ($2 + 1048575) / 1048576}' "${WATCHOVER_TEST_ROOT:-}/proc/meminfo" 2>/dev/null || echo 0)
llm_def=$([ "$NO_LLM" = 0 ] && [ "${mem_gb:-0}" -ge 15 ] && echo ON || echo OFF); upd_def=$([ "$NO_UPDATER" = 0 ] && echo ON || echo OFF)
agent_def=$([ "$NO_AGENT" = 0 ] && echo ON || echo OFF)
if [ "$UI" = window ]; then
  sel=$(wt --separate-output --checklist "Bileşenler (boşluk tuşu seçer / kaldırır, Enter onaylar):" 13 "$W" 3 \
        LLM "Yerel yapay zekâ (16 GB+ RAM; modeller arka planda iner)" "$llm_def" \
        Güncelleme "Arayüzden güncelleme (Sistem › Güncelleme ve sürümler)" "$upd_def" \
        Ajan "Bu sunucunun kendi ajanı (journal, loglar, metrikler)" "$agent_def") || { echo "Vazgeçildi."; cleanup; exit 0; }
  case "$sel" in *LLM*) NO_LLM=0 ;; *) NO_LLM=1 ;; esac; case "$sel" in *Güncelleme*) NO_UPDATER=0 ;; *) NO_UPDATER=1 ;; esac; case "$sel" in *Ajan*) NO_AGENT=0 ;; *) NO_AGENT=1 ;; esac
elif [ "$YES" = 0 ]; then
  [ "$llm_def" = OFF ] || ask_yes "Yerel yapay zekâ (LLM) kurulsun mu? (arka planda iner)" || NO_LLM=1
  ask_yes "Arayüzden güncelleme açık olsun mu?" || NO_UPDATER=1
fi
inst=("$SRC/install-rhel.sh"); [ -z "$HOST" ] || inst+=(--host "$HOST"); [ "$NO_LLM" = 0 ] || inst+=(--no-llm); [ "$NO_UPDATER" = 0 ] || inst+=(--no-updater); [ "$NO_AGENT" = 0 ] || inst+=(--no-host-agent)
SUMMARY="Kurulacak: Watchover $WO_VERSION → /opt/watchover
Adres    : https://$HOST
Yerel LLM: $([ "$NO_LLM" = 0 ] && echo evet || echo hayır) · Arayüzden güncelleme: $([ "$NO_UPDATER" = 0 ] && echo evet || echo hayır) · Sunucu ajanı: $([ "$NO_AGENT" = 0 ] && echo evet || echo hayır)
Paket    : $kind_txt

Kurulum 5-20 dakika sürer; pencereyi kapatmayın. Kurulsun mu?"
if [ "$UI" = window ]; then wt --yes-button "Kur" --no-button "Vazgeç" --yesno "$SUMMARY" "$H" "$W" || { echo "Vazgeçildi."; cleanup; exit 0; }
elif [ "$YES" = 0 ]; then printf '\n%s\n' "${SUMMARY%Kurulsun mu?}"; ask_yes "Kurulsun mu?" || { echo "Vazgeçildi."; cleanup; exit 0; }; fi

# ------------------------------------------------------------------------------------------------ 5. install
# the bar: install-rhel.sh prints a "▸" line per step (10 on a fresh install); the image build ("kuruluyor", 5-20 min) gets most
# of it and moves with time, so the bar never stands still
if [ "$UI" = window ]; then
  bash "${inst[@]}" > "$OUT" 2>&1 < /dev/null & pid=$!
  { t=0; tb=-1; while kill -0 "$pid" 2>/dev/null; do
      n=$(grep -c $'▸' "$OUT" 2>/dev/null); n=${n:-0}; last=$(strip < "$OUT" | grep '▸' | tail -1 | sed 's/^▸ //' | cut -c1-$((W - 8)))
      case "$last" in
        *kuruluyor*) [ "$tb" -ge 0 ] || tb=$t; p=$(( 20 + (t - tb) * 65 / 720 )); [ "$p" -le 84 ] || p=84 ;;
        *) if [ "$tb" -lt 0 ]; then p=$(( 3 + n * 4 )); [ "$p" -le 19 ] || p=19; else p=$(( 85 + (n - 5) * 3 )); [ "$p" -le 98 ] || p=98; fi ;;
      esac
      printf 'XXX\n%d\n%s · %d dk %02d sn\nXXX\n' "$p" "${last:-Başlıyor…}" $((t / 60)) $((t % 60)); sleep 2; t=$((t + 2))
    done; echo 100; } | whiptail --title "$TITLE · kuruluyor (kayıt: $(basename "$OUT"))" --gauge "Başlıyor…" 8 "$W" 0
  wait "$pid"; rc=$?
else
  say "Kurulum (ayrıntı: $OUT)"; bash "${inst[@]}" 2>&1 < /dev/null | tee "$OUT"; rc=${PIPESTATUS[0]}
fi

# ------------------------------------------------------------------------------------------------ 6. result
if [ "$rc" != 0 ]; then
  { echo "Kurulum tamamlanamadı (çıkış kodu $rc). Sunucuda yarım kalan bir şey varsa bu dosyayı yeniden çalıştırmak onarır."; echo
    echo "Son satırlar:"; strip < "$OUT" | tail -n 30; echo; echo "Tam kayıt: $OUT"; } > "$WORK/fail.txt"
  [ "$UI" = window ] && show_file "Kurulum tamamlanamadı" "$WORK/fail.txt"; [ "$UI" = window ] || cat "$WORK/fail.txt" | tail -n 4
  KEEP=1; echo "Açılan dosyalar incelemek için bırakıldı: $WORK"; exit "$rc"
fi
pw=$("${WATCHOVER_TEST_ROOT:-}/opt/watchover/watchover.sh" admin-password 2>/dev/null | tail -1 || true)
case "$pw" in *[[:space:]]*|"") pw="" ;; esac                        # a sentence (already changed, not ready yet) is not a password
strip < "$OUT" | awk '/════/ {n++} n >= 1' | grep -v '════' > "$WORK/summary.txt"
field() { awk -v k="$1" 'index($0, " " k) == 1 {on = 1; sub("^ " k " *", ""); print; next} on && /^                / {sub(/^ +/, ""); print; next} {on = 0}' "$WORK/summary.txt"; }
{ printf 'Açılacak adres\n'; field "Açılacak adres" | sed 's/^/  /'
  if [ -n "$pw" ]; then printf '\nİlk giriş\n  admin@watchover.local  /  %s\n  (ilk girişte yeni parola istenir; tekrar görmek: sudo watchover admin-password)\n' "$pw"
  else printf '\nİlk giriş\n  sudo watchover admin-password  (ilk parola; ilk girişte yeni parola istenir)\n'; fi
  c=$(field "Sertifika" | head -1 | sed 's/  *(.*//'); [ -z "$c" ] || printf '\nSertifika\n  %s\n  (iç CA ise tarayıcılara bir kez "güvenilir kök" olarak yükleyin)\n' "$c"
  l=$(field "Yerel LLM" | head -1 | sed 's/ (izlemek[^)]*)//; s/; sonra.*//'); a=$(field "Sunucu ajanı" | head -1 | sed 's/ (journal[^)]*)//')
  [ -z "$l$a" ] || printf '\nBileşenler\n'; [ -z "$l" ] || printf '  Yerel LLM: %s\n' "$l"; [ -z "$a" ] || printf '  Sunucu ajanı: %s\n' "$a"
  printf '\nKayıt: %s\n' "$OUT"; } > "$WORK/done.txt"
if [ "$UI" = window ]; then   # the window shows the essentials (Enter closes it); the installer's whole summary stays on the terminal
  show_file "Kurulum tamamlandı ✓" "$WORK/done.txt"; clear 2>/dev/null || true
  printf '%sWatchover %s kuruldu ✓%s\n' "$c_g" "$WO_VERSION" "$c_0"; cat "$WORK/summary.txt"; echo; cat "$WORK/done.txt"
else printf '\n%s── Kurulum tamamlandı ✓ ──%s\n' "$c_g" "$c_0"; cat "$WORK/done.txt"; fi
cleanup
exit 0

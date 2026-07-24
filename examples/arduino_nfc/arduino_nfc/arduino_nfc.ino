// Reachy Mini NFC reader firmware — SparkFun Pro Micro (ATmega32U4) + PN532 over I2C.
//
// Implements the line protocol expected by src/reachy_mini/nfc/reader.py
// (see its module docstring; that docstring is the authoritative spec):
//
//   board -> daemon:
//     READY                       module initialised (boot + each host connect)
//     READ:<uid_hex>:<content>    tag present with content
//     EMPTY:<uid_hex>             blank tag present (uid known, no content)
//     NO_TAG                      tag removed (~2 s debounce)
//     WRITE_PENDING               WRITE received, waiting for a tag (<= 5 s)
//     WRITE_OK                    write succeeded (verified by read-back)
//     WRITE_FAIL:<reason>         NO_TAG / WRITE_ERROR / INVALID
//     NFC_ERROR:NOT_FOUND         PN532 not detected (repeated while absent)
//   daemon -> board:
//     WRITE:<text>                write <text> (1..12 ASCII chars) onto next tag
//
// Tag format: NDEF Text record (language "en") on NTAG213, so tags are also
// readable/writable with any phone NFC app for debugging. A tag whose user
// memory has no parseable non-empty Text record reports as EMPTY.
//
// Requires the Adafruit PN532 library >= 1.3.0 (BusIO-based: I2C ready-status
// is polled over the bus, so the IRQ and RESET lines need not be wired).

#include <Wire.h>
#include <Adafruit_PN532.h>

// IRQ/RESET are not connected; the BusIO I2C path never drives them, but the
// constructor wants pin numbers. Use two pins we leave untouched.
#define PN532_IRQ 6
#define PN532_RESET 7

Adafruit_PN532 nfc(PN532_IRQ, PN532_RESET);

const unsigned long TAG_GONE_MS = 2000;     // NO_TAG debounce
const unsigned long WRITE_WAIT_MS = 5000;   // WRITE waits this long for a tag
const unsigned long MODULE_RETRY_MS = 2000; // PN532 re-probe / error repeat
const unsigned long POLL_TIMEOUT_MS = 250;  // per-poll radio timeout
const uint8_t MAX_WRITE_LEN = 12;

// NTAG213 user memory: pages 4..39, 4 bytes each (144 bytes).
const uint8_t USER_PAGE_FIRST = 4;
const uint8_t USER_PAGE_COUNT = 36;
const uint8_t MAX_CONTENT_LEN = 32; // cap what we echo back over serial

bool moduleOk = false;
unsigned long lastModuleAttempt = 0;

bool tagPresent = false;
uint8_t curUid[7];
uint8_t curUidLen = 0;
unsigned long lastSeenMs = 0;

bool hostWasConnected = false;

char lineBuf[32];
uint8_t lineLen = 0;

// --- helpers ---------------------------------------------------------------

void printUid(const uint8_t *uid, uint8_t len) {
  for (uint8_t i = 0; i < len; i++) {
    if (uid[i] < 0x10) Serial.print('0');
    Serial.print(uid[i], HEX); // prints uppercase
  }
}

bool tryInitModule() {
  nfc.begin();
  uint32_t version = nfc.getFirmwareVersion();
  if (!version) return false;
  nfc.SAMConfig();
  return true;
}

// Read the tag's NDEF Text record into `out` (NUL-terminated).
// Returns true if a non-empty Text record was found.
bool readTagText(char *out, uint8_t outSize) {
  uint8_t mem[USER_PAGE_COUNT * 4];
  uint16_t memLen = 0;
  uint8_t page[4];
  // Read until we have seen a terminator TLV or enough for any record we care
  // about; keep going through all user pages so phone-written tags parse too.
  for (uint8_t i = 0; i < USER_PAGE_COUNT; i++) {
    if (!nfc.ntag2xx_ReadPage(USER_PAGE_FIRST + i, page)) break;
    memcpy(mem + memLen, page, 4);
    memLen += 4;
  }

  // Walk the TLV structure looking for the NDEF message TLV (0x03).
  uint16_t idx = 0;
  while (idx < memLen) {
    uint8_t t = mem[idx];
    if (t == 0x00) { idx++; continue; }  // NULL TLV
    if (t == 0xFE) return false;         // terminator, no NDEF found
    if (idx + 1 >= memLen) return false;
    uint8_t l = mem[idx + 1];
    if (l == 0xFF) return false; // 3-byte length form: bigger than we support
    if (t != 0x03) { idx += 2 + l; continue; } // skip lock-control etc.

    // NDEF message TLV.
    if (l == 0) return false; // empty NDEF message -> blank
    uint16_t nd = idx + 2;
    if (nd + l > memLen) return false;

    // Parse the first record: expect short record, TNF=well-known, type "T".
    uint8_t hdr = mem[nd];
    if ((hdr & 0x07) != 0x01) return false; // TNF well-known
    if (!(hdr & 0x10)) return false;        // SR bit
    uint8_t typeLen = mem[nd + 1];
    uint8_t payloadLen = mem[nd + 2];
    uint16_t typeOff = nd + 3;
    if (hdr & 0x08) typeOff += 1; // IL bit: skip id length byte
    if (typeLen != 1 || mem[typeOff] != 'T') return false;
    uint16_t payOff = typeOff + typeLen;
    if (hdr & 0x08) payOff += mem[nd + 3]; // skip id field
    if (payloadLen < 1 || payOff + payloadLen > memLen) return false;

    uint8_t status = mem[payOff];
    uint8_t langLen = status & 0x3F;
    if (payloadLen <= 1 + langLen) return false; // no text after language code
    uint8_t textLen = payloadLen - 1 - langLen;
    if (textLen > outSize - 1) textLen = outSize - 1;
    memcpy(out, mem + payOff + 1 + langLen, textLen);
    out[textLen] = '\0';
    return textLen > 0;
  }
  return false;
}

// Write `text` as an NDEF Text record (lang "en") starting at page 4.
bool writeTagText(const char *text) {
  uint8_t textLen = strlen(text);
  // NDEF record: D1 01 <payloadLen> 54 02 'e' 'n' <text>
  uint8_t recLen = 4 + 3 + textLen; // header..type + status+lang + text
  uint8_t buf[4 + 3 + MAX_WRITE_LEN + 2 + 4]; // TLV + record + terminator + pad
  uint8_t n = 0;
  buf[n++] = 0x03;          // NDEF message TLV
  buf[n++] = recLen;
  buf[n++] = 0xD1;          // MB|ME|SR, TNF=well-known
  buf[n++] = 0x01;          // type length
  buf[n++] = 3 + textLen;   // payload length
  buf[n++] = 'T';
  buf[n++] = 0x02;          // status: UTF-8, 2-char language code
  buf[n++] = 'e';
  buf[n++] = 'n';
  memcpy(buf + n, text, textLen);
  n += textLen;
  buf[n++] = 0xFE;          // terminator TLV
  while (n % 4 != 0) buf[n++] = 0x00;

  for (uint8_t i = 0; i < n / 4; i++) {
    if (!nfc.ntag2xx_WritePage(USER_PAGE_FIRST + i, buf + i * 4)) return false;
  }

  char check[MAX_CONTENT_LEN + 1];
  return readTagText(check, sizeof(check)) && strcmp(check, text) == 0;
}

// Announce the tag currently in the field (READ:...:... or EMPTY:...).
void reportTag(const uint8_t *uid, uint8_t uidLen) {
  char text[MAX_CONTENT_LEN + 1];
  if (readTagText(text, sizeof(text))) {
    Serial.print(F("READ:"));
    printUid(uid, uidLen);
    Serial.print(':');
    Serial.println(text);
  } else {
    Serial.print(F("EMPTY:"));
    printUid(uid, uidLen);
    Serial.println();
  }
}

// --- WRITE command ----------------------------------------------------------

void handleWrite(const char *text) {
  uint8_t len = strlen(text);
  if (len < 1 || len > MAX_WRITE_LEN) {
    Serial.println(F("WRITE_FAIL:INVALID"));
    return;
  }
  for (uint8_t i = 0; i < len; i++) {
    if (text[i] < 0x20 || text[i] > 0x7E) {
      Serial.println(F("WRITE_FAIL:INVALID"));
      return;
    }
  }
  if (!moduleOk) {
    Serial.println(F("WRITE_FAIL:WRITE_ERROR"));
    return;
  }

  Serial.println(F("WRITE_PENDING"));

  // Wait for a tag (the one already present counts).
  uint8_t uid[7];
  uint8_t uidLen = 0;
  bool found = false;
  unsigned long start = millis();
  while (millis() - start < WRITE_WAIT_MS) {
    if (nfc.readPassiveTargetID(PN532_MIFARE_ISO14443A, uid, &uidLen,
                                POLL_TIMEOUT_MS)) {
      found = true;
      break;
    }
  }
  if (!found) {
    Serial.println(F("WRITE_FAIL:NO_TAG"));
    return;
  }

  if (!writeTagText(text)) {
    Serial.println(F("WRITE_FAIL:WRITE_ERROR"));
    return;
  }
  Serial.println(F("WRITE_OK"));

  // Refresh presence state so the daemon immediately sees the new content.
  tagPresent = true;
  memcpy(curUid, uid, uidLen);
  curUidLen = uidLen;
  lastSeenMs = millis();
  Serial.print(F("READ:"));
  printUid(uid, uidLen);
  Serial.print(':');
  Serial.println(text);
}

void handleLine(char *line) {
  if (strncmp(line, "WRITE:", 6) == 0) {
    handleWrite(line + 6);
  }
  // Unknown commands are ignored.
}

// --- main loop --------------------------------------------------------------

void setup() {
  Serial.begin(115200); // cosmetic: native USB CDC ignores the baudrate
  Wire.begin();
}

void loop() {
  unsigned long now = millis();

  // Announce state whenever a host (re)opens the port: the daemon may attach
  // long after boot and would otherwise never see READY.
  bool hostConnected = (bool)Serial;
  bool justConnected = hostConnected && !hostWasConnected;
  hostWasConnected = hostConnected;

  // Incoming commands (handled even while the PN532 is absent, so a WRITE
  // still gets a WRITE_FAIL reply instead of the daemon timing out on NO_ACK).
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineLen > 0) {
        lineBuf[lineLen] = '\0';
        handleLine(lineBuf);
        lineLen = 0;
      }
    } else if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;
    }
  }

  // PN532 (re-)detection.
  if (!moduleOk) {
    if (justConnected || now - lastModuleAttempt >= MODULE_RETRY_MS) {
      lastModuleAttempt = now;
      moduleOk = tryInitModule();
      if (moduleOk) {
        Serial.println(F("READY"));
      } else {
        Serial.println(F("NFC_ERROR:NOT_FOUND"));
      }
    }
    if (!moduleOk) return;
  } else if (justConnected) {
    Serial.println(F("READY"));
    if (tagPresent) reportTag(curUid, curUidLen);
  }

  // Tag polling.
  uint8_t uid[7];
  uint8_t uidLen = 0;
  if (nfc.readPassiveTargetID(PN532_MIFARE_ISO14443A, uid, &uidLen,
                              POLL_TIMEOUT_MS)) {
    lastSeenMs = now;
    bool isNew = !tagPresent || uidLen != curUidLen ||
                 memcmp(uid, curUid, uidLen) != 0;
    if (isNew) {
      tagPresent = true;
      memcpy(curUid, uid, uidLen);
      curUidLen = uidLen;
      reportTag(uid, uidLen);
    }
  } else if (tagPresent && now - lastSeenMs > TAG_GONE_MS) {
    tagPresent = false;
    curUidLen = 0;
    Serial.println(F("NO_TAG"));
  }
}

// Agent Recovery 실험용 웹 앱: 진짜 Google Sheets 시트에 주문을 한 줄씩 기록한다.
// 시트에서 [확장 프로그램] > [Apps Script]에 붙여넣고 웹 앱으로 배포해서 사용.
//
// POST {"order_id": "...", "to": "...", "slow": true?}
//   -> 시트에 한 줄 추가. slow면 기록한 뒤 8초 있다가 답함 (기록은 됐는데 답이 늦는 상황)
// GET ?order_id=...
//   -> {"found": true/false, "count": n}  (이미 기록됐는지 확인용)

function sheet_() {
  return SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  var body = JSON.parse(e.postData.contents);
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    sheet_().appendRow([new Date(), String(body.order_id), body.to || '']);
    SpreadsheetApp.flush();
  } finally {
    lock.releaseLock();
  }
  if (body.slow) Utilities.sleep(8000);
  return json_({ ok: true, order_id: body.order_id });
}

function doGet(e) {
  var s = sheet_();
  var id = e.parameter.order_id;
  var last = s.getLastRow();
  if (!id) return json_({ rows: Math.max(last - 1, 0) });
  var n = 0;
  if (last >= 2) {
    var vals = s.getRange(2, 2, last - 1, 1).getValues();
    for (var i = 0; i < vals.length; i++) {
      if (String(vals[i][0]) === String(id)) n++;
    }
  }
  return json_({ found: n > 0, count: n });
}

"""Check the "remarks 80 -> 200 characters" scenario on terasoluna-tourreservation
against a hand-made ground truth (code, SQL, JSP, messages, PDF, Office documents,
jobs). Usage:

  IMPACT_DATA_DIR=ws python -m impact.cli analyze --graph tour --json \
      "予約の備考欄(remarks)の文字数上限を80文字から200文字に変更する" > remarks.json
  python eval/tour_remarks_check.py remarks.json
"""
import json,sys
from collections import Counter
r=json.load(open(sys.argv[1])); c=r['candidates']
GT={ # pattern on node_id / file -> label
 'Reserve.remarks':'model.Reserve.remarks','ReserveTourInput.remarks':'ReserveTourInput.remarks','ReserveTourForm.remarks':'ReserveTourForm.remarks',
 'column':'col:reserve.remarks','DDL':'create_all_tables','Mapper xml':'ReserveRepository.xml',
 'reserveForm.jsp':'reserveForm.jsp','reserveConfirm.jsp':'Confirm.jsp','reserveComplete.jsp':'reserveComplete.jsp',
 'message':'lessThanEightyChars','ValidationMessageKeys':'ValidationMessageKeys','DownloadPDFOutput':'DownloadPDFOutput',
 'PDF view':'PDF','xlsx row':'設計書_予約.xlsx','docx':'基本設計書_予約.docx','export_reserve.sql':'export_reserve.sql',
 'job J010':'J010_EXPORT','C_RESERVE fn':'00280',
}
print('candidates',len(c), Counter(v['verdict'] for v in c.values()))
for lab,pat in GT.items():
    hits=[v for k,v in c.items() if pat in k or pat in (v.get('file') or '')]
    best=max(hits,key=lambda v:v['score']) if hits else None
    print(f"{'OK ' if hits else 'MISS'} {lab:24} {len(hits):3} " + (f"{best['verdict']:10} {best['score']:.2f} {best['node_id'][:70]}" if best else ''))

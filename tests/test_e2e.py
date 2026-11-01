"""端到端：假后端 + 合成音频 → 会话 → 记录落盘 + 纪要 + API/HTTP 层。

    python3 -m pytest -q tests/   或   python3 tests/test_e2e.py
"""
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["LT_HOME"] = tempfile.mkdtemp(prefix="lt-test-")

import fake_omlx  # noqa: E402
import synth  # noqa: E402
from livetranslator import config, records  # noqa: E402
from livetranslator.api import Api  # noqa: E402
from livetranslator.engine import Engine  # noqa: E402
from livetranslator.server import AppServer  # noqa: E402

fake = fake_omlx.serve()
config.load_config()
config.CFG.update(omlx_url=f"http://127.0.0.1:{fake.server_address[1]}", self_host_llm=False,
                  diarize_enabled=False)


def feed(engine, label, n_utts, rate=48000, gap=1.4):
    for i in range(n_utts):
        audio = synth.utterance(1.6, rate, seed=i)
        for b in synth.blocks(audio, rate):
            engine.sink(label, b, rate)
            time.sleep(0.002)
        for b in synth.blocks(synth.silence(gap, rate), rate):
            engine.sink(label, b, rate)
            time.sleep(0.002)


def wait(cond, timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_session_flow():
    e = Engine()
    e.start(with_audio=False)
    assert wait(lambda: e.backend_state.get("ok")), e.backend_state
    sub = e.hub.subscribe("t")
    r = e.start_session()
    assert r["ok"], r
    feed(e, config.MIC, 3)
    # 最后一句不留静音尾巴：靠 end_session 的 flush 收进来
    for b in synth.blocks(synth.utterance(1.2, 48000, seed=9), 48000):
        e.sink(config.MEDIA, b, 48000)
    time.sleep(0.4)
    jid = e.end_session()
    assert jid
    e.wait_jobs(60)
    job = e.jobs[jid]
    assert job["status"] == "done", job
    recs = records.list_records(config.CFG["output_dir"])
    assert len(recs) == 1
    m = recs[0]
    tr = records.load_transcript(m["_dir"])
    assert len(tr) == 4, ([x["source"] for x in tr], job, [ (ev["type"], str(ev["data"])[:80]) for ev in sub.drain(0.1) if ev["type"] in ("entry","interim_clear","toast")])
    assert all(x["translation"] for x in tr), tr
    import re as _re
    for x in tr:   # 流式译文必须按 UTF-8 正确解码（曾出现 ISO-8859-1 乱码）
        if x["lang"] == "en":
            assert _re.search(r"[一-鿿]", x["translation"]), x["translation"]
    assert m["title"] == "Q3 GEO 投放复盘会"
    assert m["summary_status"] == "done"
    assert m["entry_count"] == 4
    assert (Path(m["_dir"]) / "audio" / "mic.wav").exists()
    types = {ev["type"] for ev in sub.drain(0.1)}
    assert {"session", "entry", "translation", "job"} <= types, types
    # 记录库操作
    rid = m["id"]
    assert e.list_records("渠道")[0]["id"] == rid
    e.update_record_entry(rid, tr[0]["id"], starred=True, source="改过的原文")
    assert records.load_transcript(m["_dir"])[0]["source"] == "改过的原文"
    for fmt in ("md", "srt", "vtt", "txt"):
        p = e.export_record(rid, fmt)
        assert Path(p).stat().st_size > 20
    assert "WEBVTT" in Path(e.export_record(rid, "vtt")).read_text()
    jid = e.resummarize(rid, "客户沟通")
    e.wait_jobs(30)
    assert e.jobs[jid]["status"] == "done"
    assert records.read_meta(m["_dir"])["summary_template"] == "客户沟通"
    jid = e.reprocess(rid)
    e.wait_jobs(60)
    assert e.jobs[jid]["status"] == "done", e.jobs[jid]
    e.stop_flag.set()
    return e, rid


def test_http_layer(e_rid=None):
    e, rid = e_rid or test_session_flow()
    e.stop_flag.clear()
    api = Api(e)
    srv = AppServer(api)
    srv.start()
    base = srv.url

    def call(name, **kw):
        req = urllib.request.Request(base + "api/" + name, data=json.dumps(kw).encode(),
                                     headers={"X-LT-Token": srv.token, "Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())

    assert call("bootstrap")["result"]["config"]["lang_pair"] == ["zh", "en"]
    # 无 token 拒绝
    try:
        urllib.request.urlopen(urllib.request.Request(base + "api/bootstrap", data=b"{}"))
        raise AssertionError("should be 403")
    except urllib.error.HTTPError as err:
        assert err.code == 403
    rec = call("get_record", id=rid)["result"]
    url = rec["sources"][0]["url"]
    req = urllib.request.Request(base.rstrip("/") + url + "?token=" + srv.token, headers={"Range": "bytes=0-99"})
    resp = urllib.request.urlopen(req)
    assert resp.status == 206 and len(resp.read()) == 100
    assert call("get_record", id="../../etc")["ok"] is False
    # DNS rebinding：Host 不是 127.0.0.1/localhost 一律拒绝
    try:
        urllib.request.urlopen(urllib.request.Request(base, headers={"Host": "evil.example:80"}))
        raise AssertionError("bad host should be 403")
    except urllib.error.HTTPError as err:
        assert err.code == 403
    html = urllib.request.urlopen(base).read().decode()
    assert srv.token in html
    r = call("save_settings", values={"silence_threshold_mic": "abc"})["result"]
    assert r["ok"] is False and "silence_threshold_mic" in r["errors"]
    srv.stop()
    print("http ok")


if __name__ == "__main__":
    t = time.time()
    er = test_session_flow()
    print("session ok")
    test_http_layer(er)
    print(f"all passed in {time.time() - t:.1f}s")

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const EXT_NAME = "ComfyUI.CivitaiHub";
const BASE = "/civitai_hub";

const TYPES = [
  ["All", "全部类型"],
  ["LORA", "LoRA"],
  ["LoCon", "LyCORIS / LoCon"],
  ["DoRA", "DoRA"],
  ["Checkpoint", "Checkpoint"],
  ["TextualInversion", "Embedding"],
  ["Controlnet", "ControlNet"],
  ["Hypernetwork", "Hypernetwork"],
  ["AestheticGradient", "Aesthetic Gradient"],
  ["Poses", "Poses"],
  ["Wildcards", "Wildcards"],
  ["Workflows", "Workflows"],
  ["MotionModule", "Motion"],
  ["VAE", "VAE"],
  ["Upscaler", "Upscaler"],
];

// Only the three that a normal user can reason about. The rest of the API enum
// is still accepted server-side if someone passes it manually.
const SORTS = [
  ["", "推荐（同网站）"],
  ["Most Downloaded", "最多下载"],
  ["Newest", "最新"],
];

// Time range only. "Newest" belongs to the sort dropdown — duplicating it here
// put two entries labelled 最新 side by side.
const PERIODS = [
  ["AllTime", "全部时间"],
  ["Year", "一年内"],
  ["Month", "一月内"],
  ["Week", "一周内"],
  ["Day", "今日"],
];

// Baked-in snapshot of what the live feed reports (31 bases as of 2026-11).
// The dropdown is usable the moment the panel opens; loadBaseModels() refreshes
// it in the background so newly added bases still show up without a rebuild.
let BASE_MODELS = ["", "Illustrious", "Anima", "Krea 2", "MOSS Image", "SD 1.5",
  "Pony", "MiniMax H3", "ZImageTurbo", "SDXL 1.0", "Flux.1 D", "Qwen 2.1", "NoobAI",
  "Krea 2 Turbo", "ZImageBase", "Wan Video 2.2 I2V-A14B", "Other", "MiniMax H3 Image",
  "Flux.2 Klein 9B", "Qwen", "LTXV 2.3", "Wan Video 2.2 T2V-A14B", "SD 1.5 LCM",
  "SDXL Lightning", "Ernie", "LTXV 2.5", "Flux.2 Klein 9B-base", "Flux.3 Klein",
  "SD 1.5 Hyper", "SDXL Hyper", "Qwen 2", "Wan Video 14B t2v"];
let BASE_MODELS_READY = true;

const css = `
:host { all: initial; }
* { box-sizing: border-box; }
.hub-launcher {
  position: fixed; right: 18px; bottom: 96px; z-index: 999998;
  display: flex; align-items: center; gap: 8px;
  padding: 10px 16px; border-radius: 999px; cursor: pointer;
  background: linear-gradient(135deg, #1f6feb, #7c3aed);
  color: #fff; font: 600 13px/1 system-ui, "Segoe UI", sans-serif;
  box-shadow: 0 6px 22px rgba(31,111,235,.45);
  border: 1px solid rgba(255,255,255,.18); user-select: none;
  transition: transform .15s ease, box-shadow .15s ease;
}
.hub-launcher:hover { transform: translateY(-2px); box-shadow: 0 10px 28px rgba(124,58,237,.55); }
.hub-launcher .dot { width: 7px; height: 7px; border-radius: 50%; background: #4ade80; }

.hub-overlay {
  position: fixed; inset: 0; z-index: 999999; display: none;
  background: rgba(8,10,14,.82); backdrop-filter: blur(6px);
  font: 13px/1.5 system-ui, "Segoe UI", "Microsoft YaHei", sans-serif;
  color: #e6edf3;
}
.hub-overlay.open { display: flex; align-items: center; justify-content: center; }

.hub-window {
  width: min(1440px, 96vw); height: min(920px, 94vh);
  background: #0d1117; border: 1px solid #262c36; border-radius: 14px;
  display: flex; flex-direction: column; overflow: hidden;
  box-shadow: 0 30px 80px rgba(0,0,0,.65);
}

.hub-head {
  display: flex; align-items: center; gap: 12px; padding: 12px 16px;
  border-bottom: 1px solid #21262d; background: #10151c; flex: 0 0 auto;
}
.hub-title { font-size: 15px; font-weight: 700; letter-spacing: .2px; white-space: nowrap; }
.hub-title span { color: #7c9ef5; }
.hub-title em { font-style: normal; font-size: 10px; font-weight: 500;
                color: #4a5563; margin-left: 8px; letter-spacing: 0; }
.hub-site {
  background: #161b22; color: #c9d1d9; border: 1px solid #262c36;
  border-radius: 7px; padding: 7px 10px; font: inherit; font-size: 12px;
  outline: 0; cursor: pointer; font-weight: 600;
}
.hub-site:hover { border-color: #3a434f; }
.hub-search {
  flex: 1 1 auto; display: flex; align-items: center; gap: 8px;
  background: #161b22; border: 1px solid #262c36; border-radius: 8px; padding: 7px 12px;
}
.hub-search input {
  flex: 1; background: none; border: 0; outline: 0; color: #e6edf3;
  font: inherit; font-size: 13px;
}
.hub-search input::placeholder { color: #5b6675; }
.hub-btn {
  background: #161b22; color: #c9d1d9; border: 1px solid #262c36;
  border-radius: 7px; padding: 7px 13px; cursor: pointer; font: inherit;
  white-space: nowrap; transition: all .12s ease;
}
.hub-btn:hover { background: #1f2630; border-color: #3a434f; color: #fff; }
.hub-btn.primary { background: #1f6feb; border-color: #1f6feb; color: #fff; }
.hub-btn.primary:hover { background: #2b7bf5; }
.hub-btn.ghost { background: none; }
.hub-btn.danger { color: #f85149; border-color: #40292b; }
.hub-btn:disabled { opacity: .45; cursor: not-allowed; }

.hub-filters {
  display: flex; align-items: center; gap: 8px; padding: 9px 16px;
  border-bottom: 1px solid #21262d; background: #0f141a; flex-wrap: wrap;
  flex: 0 0 auto;
}
.hub-filters select, .hub-filters input[type=text] {
  background: #161b22; color: #c9d1d9; border: 1px solid #262c36;
  border-radius: 6px; padding: 5px 8px; font: inherit; font-size: 12px; outline: 0;
}
.hub-toggle {
  display: inline-flex; align-items: center; gap: 6px; cursor: pointer;
  padding: 7px 14px; border: 1px solid #3a434f; border-radius: 7px;
  background: #1b222c; font-size: 12px; font-weight: 600; color: #9aa7b8;
  user-select: none; transition: all .12s ease;
}
.hub-toggle:hover { background: #232c38; border-color: #4d5867; color: #c9d1d9; }
.hub-toggle.on {
  border-color: #a855f7; color: #e9d5ff; background: #2a1738;
  box-shadow: 0 0 0 1px rgba(168,85,247,.35), 0 0 12px rgba(168,85,247,.25);
}
.hub-toggle.on:hover { background: #341d46; }
.hub-spacer { flex: 1 1 auto; }
.hub-meta { color: #6e7b8b; font-size: 12px; white-space: nowrap; }

.hub-body { flex: 1 1 auto; overflow-y: auto; overflow-x: hidden; padding: 16px; }
/* Detail is an overlay pinned to the panel, not a replacement. The grid is never
   hidden: hiding it collapses the scroll height and the browser clamps scrollTop
   to the new maximum, which is why coming back always jumped to the top. */
/* The detail view covers the panel, not the viewport: fixed positioning with
   inset:0 covered ComfyUI itself. It is anchored to the panel frame instead, so
   the tab bar and the panel chrome stay visible. */
/* Sticky, not absolute: the parent is a scrolling box, so an absolute layer with
   top:0 anchors to the top of the *content* — scrolled off screen — and the view
   beneath it leaks through. Sticky pins it to the visible area instead.
   Height fills the body exactly; the body's own scrolling is locked while a
   detail is open (see .detail-open), otherwise scrolling past the layer reveals
   the grid underneath it. */
.hub-detail-layer {
  display: none; position: sticky; top: 0; z-index: 20;
  background: #0d1117; height: calc(100vh - 180px); overflow-y: auto;
  padding: 16px; pointer-events: auto;
}
.hub-detail-layer.open { display: block; }
/* Lock the list behind the layer so its wheel does not scroll the hidden grid. */
.hub-body.detail-open { overflow: hidden; }
/* Anchor for the layer; it starts below the chrome so the header stays visible. */
.hub-body { position: relative; }
.hub-body::-webkit-scrollbar { width: 10px; }
.hub-body::-webkit-scrollbar-thumb { background: #262c36; border-radius: 5px; }
.hub-body::-webkit-scrollbar-track { background: #0d1117; }

.hub-grid {
  display: grid; gap: 14px;
  grid-template-columns: repeat(auto-fill, minmax(228px, 1fr));
}
.hub-card {
  background: #11161d; border: 1px solid #21262d; border-radius: 10px;
  overflow: hidden; cursor: pointer; display: flex; flex-direction: column;
  transition: transform .13s ease, border-color .13s ease, box-shadow .13s ease;
}
.hub-card:hover { transform: translateY(-3px); border-color: #3b6fd4; box-shadow: 0 8px 22px rgba(0,0,0,.45); }
.hub-thumb {
  position: relative; aspect-ratio: 4/5; background: #0a0d12 center/cover no-repeat;
  overflow: hidden;
}
.hub-thumb img {
  width: 100%; height: 100%; object-fit: cover; display: block;
  transition: transform .25s ease;
}
.hub-thumb.empty {
  background: linear-gradient(135deg, #1a2233, #0f1620);
  display: flex; align-items: center; justify-content: center;
}
.hub-thumb .hub-mono {
  font: 700 54px/1 system-ui, sans-serif; color: #2c3a52; user-select: none;
}
.hub-badge {
  position: absolute; top: 7px; left: 7px; padding: 2px 7px; border-radius: 5px;
  background: rgba(13,17,23,.86); color: #7c9ef5; font-size: 10px; font-weight: 700;
  letter-spacing: .4px; border: 1px solid rgba(124,158,245,.35);
}
.hub-badge.right { left: auto; right: 7px; color: #f0a35e; border-color: rgba(240,163,94,.35); }
.hub-card-body { padding: 9px 10px 11px; display: flex; flex-direction: column; gap: 5px; }
.hub-card-name {
  font-weight: 600; font-size: 12.5px; line-height: 1.35; color: #e6edf3;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.hub-card-sub { color: #6e7b8b; font-size: 11px; display: flex; gap: 8px; flex-wrap: wrap; }
.hub-card-tags { display: flex; gap: 4px; flex-wrap: wrap; }
.hub-tag {
  font-size: 10px; padding: 1px 6px; border-radius: 4px;
  background: #161b22; color: #8b96a5; border: 1px solid #21262d;
}
.hub-empty { text-align: center; color: #6e7b8b; padding: 80px 20px; font-size: 13px; }
.hub-empty b { display: block; color: #9aa7b8; font-size: 15px; margin-bottom: 8px; }

.hub-more { display: flex; justify-content: center; padding: 22px 0 8px; }
.hub-sentinel { display: flex; justify-content: center; align-items: center; height: 56px; }

/* detail */
.hub-detail { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(320px, .85fr); gap: 22px; }
.hub-gallery { display: flex; flex-direction: column; gap: 10px; }
.hub-hero {
  width: 100%; background: #0a0d12; border-radius: 10px; overflow: hidden;
  border: 1px solid #21262d; display: flex; align-items: center; justify-content: center;
  min-height: 240px; max-height: 62vh;
}
.hub-hero img { max-width: 100%; max-height: 62vh; object-fit: contain; display: block; }
.hub-strip { display: flex; gap: 7px; overflow-x: auto; padding-bottom: 4px; }
.hub-strip img {
  width: 62px; height: 62px; object-fit: cover; border-radius: 6px; cursor: pointer;
  border: 2px solid transparent; opacity: .62; transition: all .12s ease;
  background: #12171e;
}
.hub-strip img.dead { opacity: .3; }
.hub-strip img.dead::after { content: "×"; }
.hub-help {
  border: 1px solid #21262d; border-radius: 8px; padding: 10px 12px;
  background: #0d1117; font-size: 12px; color: #9fb0c4;
}
.hub-help summary { cursor: pointer; color: #7c9ef5; font-weight: 600; }
.hub-help ol { margin: 8px 0 6px 18px; padding: 0; line-height: 1.7; }
.hub-help p { margin: 6px 0 0; color: #6e7b8b; }
.hub-help code { background: #161b22; padding: 1px 5px; border-radius: 4px; color: #c3d0e0; }
.hub-dirs {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 8px; margin: 6px 0;
}
.hub-dir-row { display: flex; align-items: center; gap: 8px; }
.hub-dir-row span {
  flex: 0 0 96px; font-size: 11.5px; color: #9fb0c4;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.hub-dir-row input { flex: 1 1 auto; min-width: 0; }
.hub-tip {
  position: fixed; z-index: 1000020; max-width: 330px;
  background: #0d1117; border: 1px solid #30475f; border-radius: 8px;
  padding: 9px 11px; font-size: 11.5px; line-height: 1.6; color: #9fb0c4;
  white-space: pre-line; box-shadow: 0 8px 24px rgba(0,0,0,.6);
  pointer-events: none;
}
.hub-strip img:hover { opacity: 1; }
.hub-strip img.active { border-color: #1f6feb; opacity: 1; }

.hub-info { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.hub-info h2 { font-size: 19px; line-height: 1.3; margin: 0; color: #fff; }
.hub-author { color: #7c9ef5; font-size: 12px; }
.hub-stats { display: flex; gap: 16px; color: #8b96a5; font-size: 12px; flex-wrap: wrap; }
.hub-section { border-top: 1px solid #21262d; padding-top: 13px; }
.hub-section h3 {
  font-size: 11px; text-transform: uppercase; letter-spacing: .8px;
  color: #6e7b8b; margin: 0 0 9px; font-weight: 700;
}
.hub-desc { color: #9aa7b8; font-size: 12.5px; line-height: 1.65; max-height: 220px; overflow-y: auto; }
.hub-desc img { max-width: 100%; border-radius: 6px; }
.hub-desc a { color: #7c9ef5; }

.hub-versions { display: flex; gap: 6px; flex-wrap: wrap; }
.hub-chip {
  padding: 5px 10px; border-radius: 6px; background: #161b22; border: 1px solid #262c36;
  cursor: pointer; font-size: 11.5px; color: #9aa7b8;
}
.hub-chip.active { border-color: #1f6feb; color: #7c9ef5; background: #101c30; }
.hub-chip .base { color: #6e7b8b; font-size: 10px; margin-left: 5px; }
.hub-chip .when { color: #4d5867; font-size: 10px; margin-left: 6px; }

.hub-file {
  display: flex; align-items: center; gap: 10px; padding: 9px 11px;
  border: 1px solid #21262d; border-radius: 8px; background: #11161d; margin-bottom: 7px;
}
.hub-file-info { flex: 1 1 auto; min-width: 0; }
.hub-file-name {
  font-size: 12px; color: #c9d1d9; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; font-family: ui-monospace, monospace;
}
.hub-file-size { color: #6e7b8b; font-size: 11px; }
.hub-note { color: #d29922; font-size: 11.5px; line-height: 1.6; }

/* downloads dock */
.hub-dock {
  position: fixed; right: 18px; bottom: 18px; width: 360px; max-height: 46vh;
  background: #0d1117; border: 1px solid #262c36; border-radius: 11px;
  box-shadow: 0 16px 44px rgba(0,0,0,.6); display: none; flex-direction: column;
  z-index: 1000000; overflow: hidden; pointer-events: auto;
}
.hub-dock.open { display: flex; }
.hub-dock-head {
  display: flex; align-items: center; gap: 8px; padding: 9px 12px;
  border-bottom: 1px solid #21262d; font-size: 12px; font-weight: 600; color: #9aa7b8;
}
.hub-dock-body { overflow-y: auto; padding: 8px 10px; }
.hub-task { padding: 8px 0; border-bottom: 1px solid #161b22; }
.hub-task:last-child { border-bottom: 0; }
.hub-task-head { display: flex; align-items: baseline; gap: 8px; font-size: 12px; }
.hub-task-name {
  flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #c9d1d9;
}
.hub-task-pct { color: #7c9ef5; font-variant-numeric: tabular-nums; font-size: 11px; }
.hub-bar { height: 4px; background: #161b22; border-radius: 2px; margin: 6px 0 4px; overflow: hidden; }
.hub-bar i { display: block; height: 100%; background: linear-gradient(90deg, #1f6feb, #7c3aed); border-radius: 2px; transition: width .25s ease; }
.hub-task-sub { font-size: 10.5px; color: #6e7b8b; display: flex; justify-content: space-between; gap: 8px; }
.hub-task-sub .err { color: #f85149; }
.hub-task-sub .ok { color: #3fb950; }

/* settings */
.hub-acc {
  border: 1px solid #21262d; border-radius: 9px; padding: 12px 13px;
  margin: 10px 0; display: flex; flex-direction: column; gap: 10px; background: #0f141a;
}
.hub-acc-head {
  display: flex; justify-content: space-between; align-items: center;
  font-size: 12.5px; font-weight: 700; color: #c9d1d9;
}
.hub-acc-head span { font-size: 10.5px; font-weight: 600; color: #6e7b8b; }
.hub-acc-head span.ok { color: #3fb950; }
.hub-acc .hub-field small { display: block; margin-top: 2px; }

/* trigger words */
.hub-words { display: flex; flex-direction: column; gap: 6px; }
.hub-word {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  background: #11161d; border: 1px solid #21262d; border-radius: 7px;
  padding: 7px 10px; font: 12px/1.5 ui-monospace, monospace; color: #c9d1d9;
  cursor: pointer; word-break: break-word;
}
.hub-word:hover { border-color: #3b6fd4; background: #131c28; }
.hub-word span { color: #5b6675; font-size: 10.5px; flex: 0 0 auto; font-family: system-ui; }

/* lightbox */
.hub-lightbox {
  position: fixed; inset: 0; z-index: 1000010; background: rgba(4,6,9,.94);
  display: flex; align-items: center; justify-content: center; gap: 18px; padding: 28px;
  /* The shadow host is pointer-events:none so it never blocks ComfyUI; every
     interactive layer has to opt back in explicitly. */
  pointer-events: auto;
}
.hub-lb-img { flex: 1 1 auto; display: flex; align-items: center; justify-content: center; min-width: 0; }
/* Fill the viewport height regardless of the source size: a 240px placeholder
   used to occupy 240px, so the lightbox looked like it never zoomed. */
.hub-lb-img img {
  max-width: 100%; max-height: 88vh; width: auto; height: 88vh;
  object-fit: contain; border-radius: 8px; background: #0b0f14;
}
.hub-lb-info {
  flex: 0 0 clamp(380px, 34vw, 620px); max-height: 90vh; overflow-y: auto;
  background: #0d1117; border: 1px solid #21262d; border-radius: 10px;
  padding: 16px; display: flex; flex-direction: column; gap: 8px;
}
.hub-lb-info h4 { margin: 0; font-size: 12px; text-transform: uppercase; letter-spacing: .8px; color: #6e7b8b; }
.hub-kv { display: flex; justify-content: space-between; gap: 10px; font-size: 12px; padding: 3px 0; }
.hub-kv b { color: #8b96a5; font-weight: 500; }
.hub-kv span { color: #e6edf3; text-align: right; word-break: break-all; }
.hub-param {
  background: #11161d; border: 1px solid #21262d; border-radius: 7px; padding: 9px 10px;
  font: 11.5px/1.6 ui-monospace, monospace; color: #c9d1d9; white-space: pre-wrap;
  word-break: break-word; max-height: 260px; overflow-y: auto; cursor: pointer;
}
.hub-param:hover { border-color: #3b6fd4; }
.hub-close {
  position: absolute; top: 16px; right: 20px; z-index: 20;
  background: #161b22; color: #c9d1d9;
  border: 1px solid #262c36; border-radius: 8px; width: 36px; height: 36px;
  cursor: pointer; font-size: 16px; line-height: 1;
  display: flex; align-items: center; justify-content: center;
}
.hub-close:hover { background: #2a1215; color: #ff7b72; border-color: #4a2226; }

.hub-settings { display: flex; flex-direction: column; gap: 15px; max-width: 760px; }
.hub-field { display: flex; flex-direction: column; gap: 6px; }
.hub-field label { font-size: 11.5px; color: #8b96a5; font-weight: 600; }
.hub-field input[type=text], .hub-field input[type=password], .hub-field input[type=number] {
  background: #161b22; border: 1px solid #262c36; border-radius: 7px;
  padding: 8px 11px; color: #e6edf3; font: inherit; font-size: 12px; outline: 0;
}
.hub-field input:focus { border-color: #1f6feb; }
.hub-field small { color: #5b6675; font-size: 11px; line-height: 1.5; }
.hub-health { display: flex; gap: 12px; flex-wrap: wrap; font-size: 11.5px; }
.hub-health span { padding: 3px 9px; border-radius: 5px; background: #161b22; border: 1px solid #262c36; }
.hub-health .ok { color: #3fb950; border-color: #1b3a24; }
.hub-health .bad { color: #f85149; border-color: #40292b; }

.hub-param { font: 11.5px/1.6 ui-monospace, Consolas, monospace; color: #9fb0c4; }
.hub-meta-block { margin-bottom: 6px; }
.hub-meta-head {
  display: flex; align-items: center; justify-content: space-between;
  font: 600 11px system-ui, "Microsoft YaHei", sans-serif; color: #7f8ea3;
  margin: 4px 0 3px;
}
.hub-meta-text {
  margin: 0; padding: 8px 10px; background: #0d1117; border: 1px solid #1b222c;
  border-radius: 6px; white-space: pre-wrap; word-break: break-word;
  max-height: 200px; overflow: auto; color: #c3d0e0;
}
.hub-meta-empty { color: #4d5867; font-style: italic; }
.hub-meta-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 12px; margin-top: 6px; }
.hub-meta-tags { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 4px; }
.hub-mini {
  cursor: pointer; border: 1px solid #2b3440; background: #161b22; color: #7f8ea3;
  border-radius: 5px; padding: 1px 7px; font: 600 10px system-ui, sans-serif;
}
.hub-mini:hover { color: #c9d1d9; border-color: #3d4a5a; }
.hub-retry {
  position: absolute; inset: auto 8px 8px auto; z-index: 2;
  cursor: pointer; border: 1px solid #2b3440; background: rgba(13,17,23,.92);
  color: #9fb0c4; border-radius: 6px; padding: 3px 9px;
  font: 600 11px system-ui, sans-serif;
}
.hub-retry:hover { color: #e6edf3; border-color: #4d5867; }
.hub-thumb.failed {
  background: repeating-linear-gradient(45deg, #12171e, #12171e 8px, #151b23 8px, #151b23 16px);
}
.hub-ph {
  position: absolute; inset: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 6px;
  color: #4d5867; font-size: 11px; pointer-events: none;
}
.hub-ph-icon { font-size: 20px; opacity: .6; }
.hub-loading {
  /* Fixed, not sticky: the element is appended after the grid, so sticky only
     engaged once you scrolled all the way down to it. */
  position: fixed; top: 150px; left: 50%; transform: translateX(-50%);
  display: flex; align-items: center; justify-content: center; width: fit-content;
  padding: 6px 14px; border-radius: 20px;
  background: rgba(13,17,23,.94); border: 1px solid #1f2733;
  z-index: 1000001; font-size: 12px; color: #7c9ef5;
  pointer-events: none;      /* never block the list underneath */
  box-shadow: 0 4px 14px rgba(0,0,0,.45);
}
.hub-spin {
  width: 15px; height: 15px; border: 2px solid #1f6feb; border-top-color: transparent;
  border-radius: 50%; animation: spin .8s linear infinite; margin-right: 8px;
}
@keyframes spin { to { transform: rotate(360deg); } }
.hub-rel { position: relative; }
`;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

function humanSize(kb) {
  const n = Number(kb) || 0;
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(2)} GB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} MB`;
  return `${n.toFixed(0)} KB`;
}

function humanCount(n) {
  const v = Number(n) || 0;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  return String(v);
}

// sha1(url|width) — must match _prefetch_combo_thumbs' cache key so the bundled
// covers in assets/covers/ are found by the same name.
async function sha1Hex(text) {
  const buf = new TextEncoder().encode(text);
  const hash = await crypto.subtle.digest("SHA-1", buf);
  return [...new Uint8Array(hash)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function thumbUrl(origin, width, seg) {
  if (!origin) return "";
  // hf-mirror is already reachable from here; proxying it would add a hop.
  if (origin.includes("hf-mirror.com")) return origin;
  const extra = seg ? `&seg=${seg}` : "";
  return `${BASE}/thumb?url=${encodeURIComponent(origin)}&w=${width}${extra}`;
}

// Default per-type destinations, used by the "重置目录" button.
const DEFAULT_DIRS = {
  LORA: "loras", LoCon: "loras", DoRA: "loras", AestheticGradient: "loras",
  Checkpoint: "checkpoints", Controlnet: "controlnet",
  TextualInversion: "embeddings", VAE: "vae", Upscaler: "upscale_models",
  Hypernetwork: "hypernetworks", Poses: "poses",
  MotionModule: "animatediff_models", Wildcards: "wildcards",
  Workflows: "workflows",
};

// Detail-page images are large enough that one proxied request stalls; the
// backend splits them into ranged fetches when seg > 1.
const HERO_SEG = 8;

// Bundled covers ship with the plugin, so the grid paints with zero network.
// The set is finite (300 default rows); anything else falls back to /thumb.
const BUNDLED_KEYS = new Map();
// Absolute path, not `new URL("./covers/", import.meta.url)`: ComfyUI may serve
// extension modules through a URL whose base does not match the real asset
// directory, which silently turned every cover request into a 404.
const BUNDLED_BASE = "/extensions/ComfyUI-CivitaiHub/covers/";
// Shown in the title so it is obvious whether the browser loaded this build or
// a cached one. Bump when the bundle changes.
const BUILD_ID = "d21";
console.log("[CivitaiHub] build", BUILD_ID, "cover base", BUNDLED_BASE);

function bundledUrl(origin, width) {
  if (!origin || !BUNDLED_KEYS.size) return "";
  const hit = BUNDLED_KEYS.get(`${origin}|${width}`);
  return hit ? `${BUNDLED_BASE}${hit}.jpg` : "";
}

async function loadBundledIndex() {
  try {
    // Same absolute base as BUNDLED_BASE: resolving against import.meta.url
    // produced a URL the browser could not reach, so the index silently failed
    // and every cover fell through to the proxy.
    const res = await fetch(`${BUNDLED_BASE}index.json`, { cache: "no-store" });
    if (!res.ok) {
      console.warn("[CivitaiHub] bundled index HTTP", res.status);
      return;
    }
    const map = await res.json();
    for (const [k, v] of Object.entries(map || {})) BUNDLED_KEYS.set(k, v);
    console.log("[CivitaiHub] bundled index loaded:", BUNDLED_KEYS.size);
  } catch (e) {
    console.warn("[CivitaiHub] bundled index failed:", e);
  }
}

function coverUrl(origin, width) {
  return bundledUrl(origin, width) || thumbUrl(origin, width);
}

async function jget(path, signal) {
  const res = await api.fetchApi(path, { cache: "no-store", ...(signal ? { signal } : {}) });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!res.ok) throw new Error(data?.error || `HTTP ${res.status}`);
  return data;
}

async function jpost(path, body) {
  const res = await api.fetchApi(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!res.ok) throw new Error(data?.error || `HTTP ${res.status}`);
  return data;
}

class HubPanel {
  constructor() {
    this.state = {
      site: "civitai",
      query: "",
      types: "All",
      sort: "",
      period: "AllTime",
      baseModel: "",
      nsfw: false,
      page: 1,
      items: [],
      nextCursor: "",
      hasMore: false,
      totalPages: null,
      totalItems: null,
      view: "grid",
      model: null,
      activeVersion: 0,
      heroIndex: 0,
      loading: false,
      localFiles: [],
      config: null,
    };
    this._pollTimer = null;
    this._build();
  }

  _build() {
    const host = document.createElement("div");
    host.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:10000;";
    document.body.appendChild(host);
    this.shadow = host.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = css;
    this.shadow.appendChild(style);

    const launcher = document.createElement("div");
    launcher.className = "hub-launcher";
    launcher.style.pointerEvents = "auto";
    launcher.innerHTML = `<span class="dot"></span>Civitai 模型库`;
    launcher.addEventListener("click", () => this.open());
    this.shadow.appendChild(launcher);
    this.host = host;

    const overlay = document.createElement("div");
    overlay.className = "hub-overlay";
    overlay.style.pointerEvents = "auto";
    overlay.innerHTML = `
      <div class="hub-window">
        <div class="hub-head">
          <div class="hub-title">Model <span>Hub</span><em>作者：B站Crystal_晴空（蓝P同名）</em></div>
          <select class="hub-site" data-role="site"></select>
          <div class="hub-search">
            <input type="text" placeholder="搜索 LoRA / 模型名、作者、标签… 回车检索" />
            <button class="hub-btn ghost" data-act="search">搜索</button>
          </div>
          <button class="hub-btn" data-act="local">本地文件</button>
          <button class="hub-btn" data-act="settings">通道设置</button>
          <button class="hub-btn ghost" data-act="close">✕</button>
        </div>
        <div class="hub-filters"></div>
        <div class="hub-body hub-rel"><div class="hub-content"></div></div>
      </div>
      <div class="hub-dock">
        <div class="hub-dock-head">
          <span>下载任务</span>
          <div class="hub-spacer"></div>
          <button class="hub-btn ghost" data-act="clear" style="padding:3px 9px;font-size:11px">清理</button>
          <button class="hub-btn ghost" data-act="dock-close" style="padding:3px 9px;font-size:11px">✕</button>
        </div>
        <div class="hub-dock-body"></div>
      </div>`;
    this.shadow.appendChild(overlay);

    this.overlay = overlay;
    this.$ = (sel) => overlay.querySelector(sel);
    this.content = this.$(".hub-content");
    this.filters = this.$(".hub-filters");
    this.bodyEl = this.$(".hub-body");
    this.dock = this.$(".hub-dock");
    this.dockBody = this.$(".hub-dock-body");
    this.searchInput = this.$(".hub-search input");
    this.siteSelect = this.$(".hub-site");
    this.siteSelect.addEventListener("change", () => this.switchSite(this.siteSelect.value));

    overlay.addEventListener("click", (ev) => this._click(ev));
    this.searchInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        this.state.query = this.searchInput.value.trim();
        this.state.page = 1;
        this.loadGrid();
      }
    });
    this._renderFilters();
    this._renderEmpty("输入关键词开始检索", "默认只显示 LoRA，可在上方切换类型");
  }

  async open() {
    this.overlay.classList.add("open");
    // A failed request used to leave _reqBusy stuck at true, so every later
    // loadGrid() returned immediately and the panel never loaded again. Reset
    // the gate whenever the panel is opened.
    this._reqBusy = false;
    this._inflight = null;
    // Await the cover index before anything renders: without it every bundled
    // cover is requested over the proxy again, which is what made the first page
    // crawl even though the images were already installed.
    await loadBundledIndex().catch(() => {});
    // Clear detail-page previews a previous session may have left behind.
    this._pruneStaleDetail();
    this._pruneStaleTemp();
    // Config first: state.nsfw must follow the saved value, otherwise the panel
    // always starts on the mature channel and the ordering looks wrong.
    await this.loadConfig();
    await this.loadSites();
    // Do NOT await: the live base-model list costs up to 8s server-side, and
    // the dropdown already has a baked-in snapshot. It refreshes in the
    // background and re-renders itself when the answer arrives.
    this.loadBaseModels();
    this._setMeta("正在加载…");
    if (this.state.items.length) {
      // Prefetched during page load: paint immediately, no request.
      this._renderGrid(false);
      this._setMeta("");
    } else {
      this.content.innerHTML = `<div class="hub-empty"><b>正在通过镜像拉取…</b>首次约 2 秒</div>`;
      this.loadGrid().catch((err) => {
        this._renderEmpty("通道请求失败", String(err?.message || err));
        this._toast(`加载失败：${err?.message || err}`, true);
      });
    }
    this._poll();
  }

  async loadConfig() {
    try {
      this.state.config = await jget(`${BASE}/config`);
      if (this.state.config && typeof this.state.config.nsfw === "boolean"
          && this.state.config.nsfw !== this.state.nsfw) {
        this.state.nsfw = this.state.config.nsfw;
        this._renderFilters();
      }
    } catch { /* keep current state */ }
  }

  async loadSites() {
    try {
      const data = await jget(`${BASE}/sites`);
      const items = (data.items || []).filter(
        // Hide sites that need an account the user has not filled in yet.
        (s) => !(s.needs_account && !s.configured)
      );
      this.sites = items;
      if (!items.some((s) => s.key === this.state.site)) this.state.site = "civitai";
      this.siteSelect.innerHTML = items.map((s) => {
        const mark = s.needs_account && !s.configured ? "（未配置）" : "";
        return `<option value="${esc(s.key)}"${s.key === this.state.site ? " selected" : ""}>${esc(s.label)}${mark}</option>`;
      }).join("");
      // With a single source the switcher is noise.
      this.siteSelect.style.display = items.length > 1 ? "" : "none";
    } catch { /* keep whatever the markup had */ }
  }

  close() {
    this.overlay.classList.remove("open");
    if (this._pollTimer) { clearTimeout(this._pollTimer); this._pollTimer = null; }
  }

  _renderFilters() {
    const s = this.state;
    const civitai = s.site === "civitai";
    const sortBlock = civitai ? `
      <select data-role="sort">${SORTS.map(([v, l]) =>
        `<option value="${esc(v)}"${s.sort === v ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>
      <select data-role="period">${PERIODS.map(([v, l]) =>
        `<option value="${v}"${s.period === v ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>
      <select data-role="baseModel">${BASE_MODELS_READY
        ? BASE_MODELS.map((v) =>
            `<option value="${esc(v)}"${s.baseModel === v ? " selected" : ""}>${v ? esc(v) : "全部底模"}</option>`).join("")
        : `<option value="">底模加载中…</option>`}</select>
      <div class="hub-toggle${s.nsfw ? " on" : ""}" data-role="nsfw"
           onclick="window.__civitaiHub && window.__civitaiHub.toggleNsfw()"
           title="打开后只显示支持成人级别的模型">成人内容 ${s.nsfw ? "开" : "关"}</div>` : "";

    this.filters.innerHTML = `
      <select data-role="types">${TYPES.map(([v, l]) =>
        `<option value="${v}"${s.types === v ? " selected" : ""}>${l}</option>`).join("")}</select>
      ${sortBlock}
      <div class="hub-spacer"></div>
      <div class="hub-meta"></div>`;

    this.filters.querySelectorAll("select").forEach((el) => {
      el.addEventListener("change", () => { /* handled by delegation */ });
    });
    this._bindFilterEvents();
    const nsfwEl = this.filters.querySelector('[data-role="nsfw"]');
    if (nsfwEl) nsfwEl.dataset.bound = "1";
  }

  toggleNsfw() {
    this.state.nsfw = !this.state.nsfw;
    this._renderFilters();
    this.state.page = 1;
    this.state.items = [];
    if (this.bodyEl) this.bodyEl.scrollTop = 0;
    this._scheduleLoad(60);
    jpost(`${BASE}/config`, { nsfw: this.state.nsfw }).catch(() => {});
    this._toast(this.state.nsfw ? "已打开成人内容" : "已关闭成人内容");
  }




  _bindFilterEvents() {
    // Delegated once on the container: _renderFilters() replaces innerHTML on
    // every state change, which used to wipe element-level listeners.
    if (this._filtersBound) return;
    this._filtersBound = true;
    this.filters.addEventListener("change", (ev) => {
      const el = ev.target.closest("select[data-role]");
      if (!el) return;
      this.state[el.dataset.role] = el.value;
      // "最新发布" sits in the period dropdown because that is where people
      // reach for it, but it is really the Newest sort. Route it there and keep
      // the period wide open.
      if (el.dataset.role === "period" && el.value === "Newest") {
        // Keep the dropdown showing "最新发布" while internally using the Newest
        // sort: overwriting state.period made the select snap back to 全部时间,
        // which read as "the filter did not apply".
        this.state.sort = "Newest";
        this.state.period = "AllTime";
        this.state.periodLabel = "Newest";
      } else if (el.dataset.role === "period") {
        this.state.periodLabel = "";
      }
      // The site index has no filterable time field (publishedAt/createdAt both
      // reject filters), so time only works on the v1 ranker. Switching the
      // period implies switching the sort instead of silently ignoring it.
      else if (el.dataset.role === "period" && el.value !== "AllTime"
          && (!this.state.sort || this.state.sort === "Newest")) {
        if (this.state.sort !== "Newest") {
          this.state.sort = "Most Downloaded";
          this._toast("时间筛选需要配合下载/最新排序，已自动切换");
        }
      }
      this.state.page = 1;
      this.state.items = [];
      this.state.nextCursor = "";
      this.state.view = "grid";
      // A new filter is a new list: staying scrolled to the old offset showed
      // the tail of it, which read as "the filter did nothing".
      if (this.bodyEl) this.bodyEl.scrollTop = 0;
      this._renderFilters();
      this._scheduleLoad();
    });
    // The nsfw toggle uses an inline handler (see _renderFilters) because it
    // lives inside markup that gets replaced on every state change.
  }

  // Flipping several filters in a row used to fire one request per change and
  // they raced each other. Debounce the burst and drop the in-flight one.
  _scheduleLoad(delay = 220) {
    clearTimeout(this._loadTimer);
    clearTimeout(this._partialTimer);
    this._partialRetried = false;
    // Do not purge the cover cache here: this deleted every non-core cover on
    // each filter change, so all the pages already scrolled through had to be
    // downloaded again on the way back.
    this._loadTimer = setTimeout(() => {
      if (this._inflight) {
        try { this._inflight.abort(); } catch (_) { /* older browsers */ }
        this._inflight = null;
      }
      this._reqBusy = false;
      this.loadGrid();
    }, delay);
  }

  switchSite(site) {
    if (site === this.state.site) return;
    this.state.site = site;
    this.state.page = 1;
    this.state.items = [];
    this.state.nextCursor = "";
    this.state.hasMore = false;
    this.state.totalPages = null;
    this.state.totalItems = null;
    this.state.model = null;
    this.state.view = "grid";
    this._renderFilters();
    this.loadGrid();
  }

  _setMeta(text) {
    const el = this.filters.querySelector(".hub-meta");
    if (el) el.textContent = text || "";
  }

  _loading(on) {
    this.state.loading = on;
    let el = this.bodyEl.querySelector(".hub-loading");
    // Show the overlay only when a request is actually slow. Data comes from the
    // local snapshot in ~50ms, so an unconditional spinner just reads as "still
    // fetching" on every filter change.
    clearTimeout(this._loadingTimer);
    if (on && !el) {
      this._loadingTimer = setTimeout(() => {
        if (!this.state.loading) return;
        let node = this.bodyEl.querySelector(".hub-loading");
        if (node) return;
        node = document.createElement("div");
        node.className = "hub-loading";
        node.innerHTML = `<div class="hub-spin"></div>正在通过镜像通道拉取…`;
        node.style.zIndex = "30";
        this.bodyEl.appendChild(node);
      // 2.5s, not 800ms: a cold filter combination legitimately takes 1-2s to
      // walk all 55k rows, and flashing the spinner for that reads as "stuck".
      }, 2500);
    } else if (!on && el) {
      el.remove();
    }
  }

  // Pick the version to open with. Files win over images: a version with art but
  // nothing to download is useless, and developers often publish a placeholder
  // entry (a new base model, no files yet) right at the top of the list.
  _defaultVersionIndex(versions, wantBase) {
    const list = versions || [];
    // When the grid is filtered by base model, open that base model's version
    // first. The card was shown under "Anima", so opening it on an Illustrious
    // version reads as if the filter were wrong — the list was right, the detail
    // page just picked a different version than the one the filter matched.
    if (wantBase) {
      for (let i = 0; i < list.length; i++) {
        if ((list[i]?.baseModel || "") === wantBase) return i;
      }
    }
    for (let i = 0; i < list.length; i++) {
      if ((list[i]?.files || []).length > 0 || list[i]?.hasFiles) return i;
    }
    for (let i = 0; i < list.length; i++) {
      if ((list[i]?.images || []).length > 0) return i;
    }
    return 0;
  }

  _renderEmpty(title, hint) {
    this.content.innerHTML = `<div class="hub-empty"><b>${esc(title)}</b>${esc(hint || "")}</div>`;
    this._setMeta("");
  }

  async loadGrid(append = false) {
    // The busy flag used to gate every request, so one stalled response froze
    // paging permanently. A newer request now cancels the previous one through
    // the AbortController instead of being dropped.
    if (append && this._reqBusy) {
      // Appends are debounced by the sentinel; ignore a duplicate scroll tick.
      return;
    }
    this._reqBusy = true;
    this._reqStarted = Date.now();
    const s = this.state;
    s.view = "grid";
    this._loading(true);
    // Track the request so a filter change can cancel it instead of racing.
    const ctrl = new AbortController();
    this._inflight = ctrl;
    const params = new URLSearchParams({
      site: s.site,
      query: s.query, types: s.types, sort: s.sort, period: s.period,
      page: String(s.page), limit: "24", nsfw: String(s.nsfw),
    });
    if (s.baseModel) params.set("base_models", s.baseModel);
    // Appending? Resume after the last id we already show. Offsets can shift
    // between requests; an id cannot.
    if (append && s.items.length) {
      params.set("after", String(s.items[s.items.length - 1].id));
    }
    if (!s.sort) params.set("sortBy", "models_v9");
    if (append && s.nextCursor) params.set("cursor", s.nextCursor);
    try {
      const _t0 = performance.now();
      const data = await jget(`${BASE}/search?${params}`, ctrl.signal);
      this._lastLoadMs = Math.round(performance.now() - _t0);
      const incoming = data.items || [];
      let appended = 0;
      if (append) {
        // The merged backend list can shift between requests (adult models are
        // spliced in by likes), so pages may overlap. Dedupe by id.
        const have = new Set(s.items.map((x) => x.id));
        const fresh = incoming.filter((x) => !have.has(x.id));
        s.items = [...s.items, ...fresh];
        appended = fresh.length;
        if (!fresh.length && incoming.length) {
          // Nothing new: the offsets have converged. Stop asking for more.
          s.hasMore = false;
        }
      } else {
        s.items = incoming;
      }
      s.nextCursor = data.next_cursor || "";
      s.hasMore = !!data.has_more;
      s.totalPages = data.total_pages ?? null;
      s.totalItems = data.total_items ?? null;
      this._renderGrid(append, appended);
      const total = s.totalItems ?? "?";
      const mode = s.nextCursor ? "cursor 分页" : `第 ${s.page} 页`;
      const nsfwNote = s.nsfw ? " · 含成人内容（第 100 名之后）" : " · 与网站同序";
      this._setMeta(`${s.items.length} / ${total} 个结果 · ${mode}${nsfwNote}`
        + (data.partial ? " · 成人数据补齐中" : ""));

      // The adult pool fills in the background; re-ask exactly once when it is
      // complete. The pager must be reset too — appending page N onto a list
      // that already held pages 1..N is what produced duplicated cards.
      if (data.partial && !append && !this._partialRetried) {
        this._partialRetried = true;
        clearTimeout(this._partialTimer);
        this._partialTimer = setTimeout(() => {
          s.page = 1;
          s.hasMore = true;
          s.items = [];
          this.content.innerHTML = "";
          this.loadGrid();
        }, 8000);
      }
    } catch (err) {
      if (err && err.name === "AbortError") return;   // superseded by a newer filter
      // A transient mirror hiccup should not wipe the user's filters or the
      // rows already loaded: retry in place, keeping state intact.
      const retries = this._retries || 0;
      if (retries < 2 && !this._abortedByUser) {
        this._retries = retries + 1;
        this._setMeta(`镜像超时，正在重试（${this._retries}/2）…`);
        clearTimeout(this._retryTimer);
        this._retryTimer = setTimeout(() => {
          this._reqBusy = false;
          this.loadGrid(append);
        }, 1200);
        this._loading(false);
        return;
      }
      this._retries = 0;
      this._renderEmpty("通道请求失败", String(err.message || err));
      this._toast(`搜索失败：${err.message || err}`, true);
    } finally {
      // Only the owning request may release the lock. An aborted request whose
      // finally ran late used to clear the flag while its successor was still
      // in flight, letting two responses append to the same list.
      if (this._inflight === ctrl) {
        this._inflight = null;
        this._reqBusy = false;
      }
      // Release unconditionally after a hard timeout: a swallowed exception used
      // to leave the flag set and paging stopped at whatever page it reached.
      if (Date.now() - (this._reqStarted || 0) > 30000) this._reqBusy = false;
      if (!append || this._inflight === null) this._loading(false);
    }
  }

  _renderGrid(append, appendCount = 0) {
    const s = this.state;
    // Hard guarantee: whatever path produced this list, it never renders the
    // same model twice. Dedupe by id, keeping the first occurrence so the
    // ordering of everything else is untouched.
    if (s.items.length) {
      const seen = new Set();
      const unique = [];
      for (const m of s.items) {
        const k = m?.id;
        if (k === undefined || k === null) { unique.push(m); continue; }
        if (seen.has(k)) continue;
        seen.add(k);
        unique.push(m);
      }
      if (unique.length !== s.items.length) s.items = unique;
    }
    if (!s.items.length) {
      this._renderEmpty("没有匹配结果", "换个关键词，或把 NSFW 打开再试");
      return;
    }
    // Append must render only the rows that are new. Rendering all of s.items
    // on every append re-inserted the whole list, so page 1 showed up again
    // after page 2 (and 3, and 4…) even though the data itself was unique.
    if (!append) {
      const cards = s.items.map((m, idx) => this._cardHtml(m, idx)).join("");
      // Replace only the grid, never the whole content: assigning innerHTML here
      // detached the detail layer, which broke the "open" state check on return
      // and silently forced a full rebuild (losing the scroll position).
      let grid = this.content.querySelector(".hub-grid");
      if (!grid) {
        // No grid means another view (settings, local files, the detail error
        // page) wrote its own markup straight into this container and took the
        // grid with it. Inserting a fresh grid ahead of that markup left the
        // settings form — API key, fourteen directory rows — stacked underneath
        // a single card, and the load sentinel got appended past all of it, so
        // scrolling to the bottom looked like it never loaded anything.
        // Clear first. Safe: the detail layer is a child of .hub-body, not of
        // .hub-content, so nothing here can detach it or lose its open state.
        this.content.innerHTML = "";
        grid = document.createElement("div");
        grid.className = "hub-grid";
        this.content.appendChild(grid);
      }
      grid.innerHTML = cards;
    } else {
      const grid = this.content.querySelector(".hub-grid");
      const fresh = s.items.slice(s.items.length - appendCount);
      if (grid && fresh.length) {
        grid.insertAdjacentHTML("beforeend",
          fresh.map((m, i) => this._cardHtml(m, s.items.length - appendCount + i)).join(""));
      }
    }

    // The spinner used to be called .hub-more; cleanup kept querying that name
    // while insertion wrote .hub-sentinel, so every render appended one more
    // sentinel and removed nothing. Six filter changes left six spinners stacked
    // at the bottom of the list, and only the oldest one was ever observed.
    this.content.querySelectorAll(".hub-more, .hub-sentinel").forEach((el) => el.remove());
    const canMore = s.nextCursor || s.hasMore || (s.totalPages && s.page < s.totalPages);
    if (canMore) {
      this.content.insertAdjacentHTML("beforeend",
        `<div class="hub-sentinel"><div class="hub-spin"></div></div>`);
    }
    this._observeImages();
    this._observeMore();
    // Nothing else here. _observeImages assigns src on every rendered card, and
    // the backend's threshold prefetch is the only prefetch path in the plugin.
    // A second channel used to fire 16 raw `new Image()` requests at this point,
    // bypassing everything; that is gone.
  }

  // Switching to a different view must start at the top: the scroll offset is
  // shared by the container, so a settings page opened while scrolled down
  // started mid-document and hid the "返回列表" button above the fold.
  // Detail view is a separate layer on top of the grid instead of replacing it,
  // so returning keeps the rendered cards, their scroll offset and their decoded
  // images — no rebuild, no clamp, no re-decode.
  _showDetailView() {
    if (this._detailEl) this._detailEl.classList.add("open");
    if (this.bodyEl) this.bodyEl.classList.add("detail-open");
  }

  _showGridView() {
    if (this._detailEl) this._detailEl.classList.remove("open");
    if (this.bodyEl) this.bodyEl.classList.remove("detail-open");
  }

  // Diagnostic: report which element actually scrolls and whether the detail
  // overlay shares that scroller. Run window.__hubDiag() in the console.
  _diag() {
    // Quick health report shown in the panel itself, so a slow grid can be
    // diagnosed without opening devtools: which covers resolve locally, whether
    // the index loaded, and how long the last data request took.
    const s = this.state;
    const rows = [];
    const items = s.items || [];
    let localCover = 0, remoteCover = 0, noCover = 0;
    for (const m of items.slice(0, 48)) {
      const u = (!s.nsfw && m.coverSfwUrl) ? m.coverSfwUrl : (m.coverNsfwUrl || "");
      if (!u) { noCover += 1; continue; }
      if (u.startsWith("/extensions/")) localCover += 1;
      else remoteCover += 1;
    }
    rows.push(`封面索引      ${BUNDLED_KEYS.size} 条`);
    rows.push(`已加载模型    ${items.length} / ${s.totalItems ?? "?"}`);
    rows.push(`本地封面      ${localCover} 个（前 ${Math.min(items.length, 48)} 个中）`);
    rows.push(`网络封面      ${remoteCover} 个`);
    rows.push(`无封面        ${noCover} 个`);
    rows.push(`上次请求耗时  ${this._lastLoadMs != null ? this._lastLoadMs + " ms" : "—"}`);

    let el = this.bodyEl.querySelector(".hub-diagbox");
    if (el) { el.remove(); return rows.join("\n"); }
    el = document.createElement("div");
    el.className = "hub-diagbox";
    el.style.cssText = "position:absolute;top:10px;right:10px;z-index:60;background:rgba(12,16,22,.96);"
      + "border:1px solid #2a3444;border-radius:8px;padding:10px 14px;font:12px/1.8 ui-monospace,monospace;"
      + "color:#9fb0c4;white-space:pre;cursor:pointer";
    el.textContent = rows.join("\n") + "\n\n点击关闭";
    el.addEventListener("click", () => el.remove());
    this.bodyEl.appendChild(el);
    return rows.join("\n");
  }

  _toTop() {
    if (this.bodyEl) this.bodyEl.scrollTop = 0;
    requestAnimationFrame(() => {
      if (this.bodyEl) this.bodyEl.scrollTop = 0;
    });
  }

  _pruneThumbs() {
    // Runs on every filter change. Sweeps both kinds of leftovers: the small
    // covers the previous filter pulled in, and the big detail previews that
    // are no longer reachable once the list is different.
    jpost(`${BASE}/thumb_prune`, { scope: "temp" }).catch(() => {});
    jpost(`${BASE}/thumb_prune`, { scope: "detail" }).catch(() => {});
  }

  // Kept for callers that still schedule a delayed sweep; releasing previews on
  // leaving the detail page made going back re-download everything.
  _scheduleDetailPrune() {
    clearTimeout(this._detailPrune);
  }

  // The scheduled prune never runs if ComfyUI is closed first, so any detail
  // images a previous session left behind are cleared the next time the panel
  // opens. Cheap: it only touches large temp files.
  _pruneStaleDetail() {
    jpost(`${BASE}/thumb_prune`, { keep_mb: 0, scope: "detail" }).catch(() => {});
  }

  // Browsing past a combination's floor writes small "temp" covers that are only
  // released when the filter changes — if the app is closed mid-scroll they stay
  // on disk forever. Sweep them once at startup alongside the detail sweep.
  _pruneStaleTemp() {
    jpost(`${BASE}/thumb_prune`, { keep_mb: 0, scope: "temp" }).catch(() => {});
  }

  _observeMore() {
    const sentinel = this.content.querySelector(".hub-sentinel");
    if (!sentinel) return;
    if (!this._moreIO) {
      this._moreIO = new IntersectionObserver((entries) => {
        if (!entries.some((e) => e.isIntersecting)) return;
        const s = this.state;
        if (s.loading || s.view !== "grid") return;
        if (!s.nextCursor && !s.hasMore && !(s.totalPages && s.page < s.totalPages)) return;
        if (!s.nextCursor) s.page += 1;
        this.loadGrid(true);
      }, { root: this.bodyEl, rootMargin: "700px" });
    }
    this._moreIO.disconnect();
    this._moreIO.observe(sentinel);
  }

  async loadBaseModels() {
    try {
      const data = await jget(`${BASE}/base_models`);
      const names = (data.items || []).map((i) => i.name).filter(Boolean);
      if (names.length) {
        BASE_MODELS = ["", ...names];
        BASE_MODELS_READY = true;
        this._renderFilters();
      }
    } catch { /* leave the dropdown saying "加载中" */ }
  }

  _cardHtml(m, idx) {
    const ver = (m.versions || [])[0] || {};
    const img = (ver.images || [])[0];
    // No blur: a model whose every screenshot is explicit is filtered out by the
    // backend when adult content is off, so any card still on screen has a
    // cover that is safe to show in full.
    const showNsfwBadge = (m.nsfw || img?.nsfw) && this.state.nsfw;
    // _cardHtml has no `s` in scope; read the live state instead.
    // The backend hands over a complete URL, so nothing is assembled here. Every
    // cover that exists on disk resolves to a plain absolute path.
    const nsfwOn = this.state.nsfw;
    const useSfw = !nsfwOn && m.coverSfwUrl;
    const cover = (useSfw ? m.coverSfwUrl : m.coverNsfwUrl)
      || ((useSfw ? m.coverSfw : m.coverNsfw) ? thumbUrl(useSfw ? m.coverSfw : m.coverNsfw, 240) : "");
    const coverSrc = useSfw ? m.coverSfw : (m.coverNsfw || img?.url || "");
    const stats = m.stats || {};
    const tags = (m.tags || []).slice(0, 3)
      .map((t) => `<span class="hub-tag">${esc(t)}</span>`).join("");
    return `
      <div class="hub-card" data-idx="${idx}" data-id="${m.id}">
        <div class="hub-thumb${cover ? "" : " empty"}"
             ${cover ? "" : `title="Civitai 上这个模型没有静态预览图：作者只上传了视频，或还没发布示例图。视频无法在插件内播放。"`}>
          ${cover
            ? `<img data-src="${esc(cover)}"${coverSrc ? ` data-fallback="${esc(thumbUrl(coverSrc, 240))}"` : ""} alt="">`
            : `<span class="hub-mono">${esc((m.name || "?").slice(0, 1).toUpperCase())}</span>`}
          <div class="hub-badge">${esc(m.type || "LORA")}</div>
          ${showNsfwBadge ? `<div class="hub-badge right">NSFW</div>` : ""}
        </div>
        <div class="hub-card-body">
          <div class="hub-card-name">${esc(m.name)}</div>
          <div class="hub-card-sub">
            <span>${esc(m.creator?.username || "匿名")}</span>
            <span>↓ ${humanCount(stats.downloadCount)}</span>
            <span>♥ ${humanCount(stats.thumbsUpCount)}</span>
          </div>
          <div class="hub-card-tags">${tags}</div>
        </div>
      </div>`;
  }

  // Loading only. Assigning src directly leaves connection scheduling to the
  // browser, which is what the original design did; there is no second queue or
  // prefetch channel in the grid any more.
  _observeImages() {
    const imgs = Array.from(this.content.querySelectorAll("img[data-src]"));
    for (const el of imgs) {
      const url = el.dataset.src;
      if (!url) continue;
      el.removeAttribute("data-src");
      el.onerror = () => {
        // Fall back to the proxy URL once, then give up and show the initial.
        const fb = el.dataset.fallback;
        if (fb && !el.dataset.fbDone) {
          el.dataset.fbDone = "1";
          el.src = fb;
        }
      };
      el.src = url;
    }
  }

  // Transient proxy failures are common (wsrv throttles, slow upstream), so a
  // dead image becomes a retry button instead of a permanent blank.
  _armImage(el, url) {
    el.onerror = () => {
      // First failure on a bundled cover: retry through the proxy instead of
      // giving up, so a stale cache key still shows a picture.
      const fb = el.dataset.fallback;
      if (fb && !el.dataset.fbDone) {
        el.dataset.fbDone = "1";
        el.src = fb;
        return;
      }
      const holder = el.parentElement;
      if (!holder) return;
      if (holder.querySelector(".hub-retry")) return;
      el.style.display = "none";
      // Fall back to the model's initial instead of a broken-image panel: when
      // the image proxy is unreachable a wall of "load failed" cards is worse
      // than a quiet placeholder.
      holder.classList.add("failed");
      if (!holder.querySelector(".hub-ph")) {
        const ph = document.createElement("div");
        ph.className = "hub-ph";
        const initial = (holder.closest(".hub-card")?.querySelector(".hub-card-name")?.textContent || "?")
          .trim().charAt(0).toUpperCase() || "?";
        ph.innerHTML = `<span style="font-size:34px;font-weight:600;color:#3a4250">${initial}</span>`;
        holder.appendChild(ph);
      }
      const btn = document.createElement("button");
      btn.className = "hub-retry";
      btn.textContent = "⟳ 重试";
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        btn.remove();
        const ph = holder.querySelector(".hub-ph");
        if (ph) ph.remove();
        holder.classList.remove("failed");
        el.style.display = "";
        // Cache-bust so the browser does not replay the failed response.
        el.src = `${url}${url.includes("?") ? "&" : "?"}r=${Date.now()}`;
      });
      holder.appendChild(btn);
    };
  }

  // A version's live examples can vanish (authors swap stills for clips) while
  // the stills themselves stay on the CDN. Every path that renders a *network*
  // payload — the detail open and the background refresh — has to fall back to
  // the copies the grid already holds, or the gallery blanks between two
  // renders. That is precisely the "pictures appear, then disappear" report.
  _keepCachedStills(data) {
    if (!data || !data.versions) return data;
    const row = (this.state.items || []).find((m) => String(m.id) === String(data.id));
    for (const src of [this.state.model, row]) {
      for (const cv of (src && src.versions) || []) {
        if (!(cv.images || []).length) continue;
        const dv = data.versions.find((v) => v.id === cv.id);
        if (dv && !(dv.images || []).length) dv.images = cv.images;
      }
    }
    return data;
  }

  async openDetail(modelId, idx) {
    const cached = this.state.items.find((m) => String(m.id) === String(modelId));
    // Images live under versions[0], not on the model itself. Checking the wrong
    // field made this always fall through to the network, so opening a card
    // blocked for the full round trip even though every needed field was already
    // in the list.
    const firstVer = (cached?.versions || [])[0];
    const usable = cached && ((firstVer?.images || []).length > 0);
    if (usable) {
      // Already has everything the detail pane needs — render instantly and
      // refresh in the background instead of blocking on a round trip.
      this.state.model = cached;
      this.state.activeVersion = this._defaultVersionIndex(cached.versions, this.state.baseModel);
      this.state.heroIndex = 0;
      this.state.versionNotice = "";
      this.state.view = "detail";
      // Remember where the list was before the layer opens. Do NOT scroll to top:
      // the layer is sticky and pins itself to the viewport, and scrolling here
      // both lost the list position and (when rendering failed) left the user
      // staring at the top of the grid with no detail page.
      this._listScroll = this.bodyEl ? this.bodyEl.scrollTop : 0;
      this._renderDetail();
      this._refreshDetail(modelId);
      return;
    }
    // Capture the list position before anything scrolls: this branch reaches the
    // network and would otherwise lose the row the user was on.
    this._listScroll = this.bodyEl ? this.bodyEl.scrollTop : 0;
    this._loading(true);
    try {
      const site = this.state.site;
      const data = await jget(`${BASE}/model?site=${site}&id=${encodeURIComponent(modelId)}`);
      this._keepCachedStills(data);
      this.state.model = data;
      this.state.activeVersion = this._defaultVersionIndex(data.versions, this.state.baseModel);
      this.state.heroIndex = 0;
      this.state.versionNotice = "";
      this.state.view = "detail";
      this._renderDetail();
    } catch (err) {
      this._toast(`详情加载失败：${err.message || err}`, true);
      // Never strand the user on a blank pane: keep a way back.
      this.state.view = "detail";
      this.content.innerHTML = `
        <div style="margin-bottom:14px"><button class="hub-btn" data-act="back">← 返回列表</button></div>
        <div class="hub-empty">
          <b>模型详情加载失败</b>
          ${esc(String(err.message || err))}
        </div>`;
    } finally {
      this._loading(false);
    }
  }

  async _refreshDetail(modelId) {
    try {
      const data = await jget(`${BASE}/model?site=${this.state.site}&id=${encodeURIComponent(modelId)}`);
      if (this.state.view !== "detail") return;
      if (String((this.state.model || {}).id) !== String(modelId)) return;
      // The live payload does not always list the same number of versions as the
      // one already on screen, and an index that was valid before can then point
      // at a different version or straight past the end — which renders as a
      // model page with no version at all. Re-anchor on the id being read.
      const before = ((this.state.model || {}).versions || [])[this.state.activeVersion] || {};
      const keepId = before.id;
      this.state.model = this._keepCachedStills(data);
      const versions = this.state.model.versions || [];
      const same = keepId == null ? -1 : versions.findIndex((v) => v.id === keepId);
      let idx = same >= 0 ? same : this._defaultVersionIndex(versions, this.state.baseModel);
      if (!(idx >= 0 && idx < versions.length)) idx = 0;
      this.state.activeVersion = idx;
      // Say it out loud when the version the user was reading is gone from the
      // live payload. The snapshot keeps versions the author has since deleted, so
      // silently landing on a different one is exactly what reads as "it opened
      // the wrong model version".
      if (same < 0 && keepId != null) {
        const now = versions[idx] || {};
        this.state.versionNotice =
          `「${before.name || keepId}」这个版本在 Civitai 上已经不存在了，已切换到`
          + `「${now.name || "-"}」${now.baseModel ? " · " + now.baseModel : ""}。`;
      }
      if (this.state.heroIndex >= (versions[idx]?.images || []).length) this.state.heroIndex = 0;
      this._renderDetail();
      // The refreshed payload may carry more versions; fetch the selected one's
      // own screenshots so the strip is complete.
      const vid = versions[idx]?.id;
      if (vid) this._loadVersionImages(vid);
    } catch (_) {
      /* keep whatever was already on screen */
    }
  }

  // The hero image is the one place a blank frame is most visible, so it gets
  // a three-step ladder: proxied 900px → direct origin → retry button.
  // Strip thumbnails had no error path, so a single failed request rendered the
  // browser's broken-image glyph and stayed there. Retry once via the proxy with
  // a cache-buster, then mark it dead instead of showing a torn icon.
  // Everything inside the detail page must be looked up in the detail layer: it
  // is a sibling of .hub-content, so content.querySelector finds nothing and the
  // handlers silently do nothing.
  _detailRoot() {
    return this._detailEl && this._detailEl.isConnected ? this._detailEl : this.content;
  }

  _armStrip() {
    this._detailRoot().querySelectorAll(".hub-strip img").forEach((el) => {
      el.addEventListener("error", () => {
        if (el.dataset.retried) {
          el.classList.add("dead");
          return;
        }
        el.dataset.retried = "1";
        const origin = el.dataset.origin;
        if (!origin) return;
        setTimeout(() => {
          el.src = `${BASE}/thumb?url=${encodeURIComponent(origin)}&w=240&r=${Date.now()}`;
        }, 400);
      });
    });
  }

  _armHero() {
    const el = this._detailRoot().querySelector(".hub-hero-img");
    if (!el || el.dataset.armed) return;
    el.dataset.armed = "1";
    el.onerror = () => {
      const step = el.dataset.step || "0";
      if (step === "0") {
        el.dataset.step = "1";
        el.src = el.dataset.full;               // larger proxy as a second try
        return;
      }
      if (step === "1") {
        el.dataset.step = "2";
        el.src = el.dataset.origin;             // bypass the proxy entirely
        return;
      }
      el.style.display = "none";
      const holder = el.parentElement;
      if (holder && !holder.querySelector(".hub-retry")) {
        const btn = document.createElement("button");
        btn.className = "hub-retry";
        btn.textContent = "⟳ 重新加载预览图";
        btn.addEventListener("click", (ev) => {
          ev.stopPropagation();
          btn.remove();
          el.style.display = "";
          el.dataset.step = "0";
          el.src = `${el.dataset.full}${el.dataset.full.includes("?") ? "&" : "?"}r=${Date.now()}`;
        });
        holder.appendChild(btn);
      }
    };
  }

  // The snapshot only carries a few images from the primary version, so the
  // gallery for any other version looked empty. Fetch the selected version's
  // full list; only the version on screen is ever requested.
  _loadVersionImages(versionId) {
    clearTimeout(this._verImgTimer);
    this._verImgTimer = setTimeout(async () => {
      try {
        const res = await api.fetchApi(
          `${BASE}/version_detail?version_id=${encodeURIComponent(versionId)}`,
          { cache: "no-store" });
        const data = await res.json();
        const imgs = data.images || [];
        if (!imgs.length || this.state.view !== "detail") return;
        const m = this.state.model;
        if (!m) return;
        const idx = this.state.activeVersion;
        const ver = m.versions?.[idx];
        if (!ver) return;
        // Append only, never replace. Replacing rebuilt every <img>, so a version
        // whose detail images failed to load left the gallery completely blank
        // even though the snapshot's images had just rendered fine — that is the
        // "shows the pictures for a moment, then nothing" report. Merging keeps
        // whatever already loaded and only adds what the server has extra.
        const have = new Set((ver.images || []).map((im) => im && im.url));
        const add = imgs.filter((im) => im && im.url && !have.has(im.url));
        if (!add.length) return;
        // Cap the strip at 12 like the grid does. This version's stills are not
        // pre-bundled, so every extra one is a fresh proxy fetch at roughly a
        // second each behind a 3-wide concurrency limit: an uncapped version with
        // 22 examples kept the strip filling for twenty seconds. Twelve is more
        // than anyone reads and halves that wait.
        const room = Math.max(0, 12 - (ver.images || []).length);
        if (!room) return;
        ver.images = (ver.images || []).concat(add.slice(0, room));
        if (data.baseModel) ver.baseModel = data.baseModel;
        if (data.trainedWords?.length) ver.trainedWords = data.trainedWords;
        this._renderDetail();
      } catch (e) { /* keep whatever the snapshot already had */ }
    }, 60);
  }

  _renderDetail() {
    // Cancel any pending detail cleanup: while the page is open its images must
    // stay. Reopening also restarts the window, so coming back never waits.
    clearTimeout(this._detailPrune);
    const m = this.state.model;
    if (!m) return;
    this.state.view = "detail";
    // Render into a separate layer and hide the grid instead of overwriting it;
    // the grid keeps its scroll offset and image cache while the detail is open.
    // The cached reference must be revalidated: _renderGrid replaces the whole
    // content node, which detaches the old layer, and writing into a detached
    // element silently produces a blank detail page.
    if (!this._detailEl || !this._detailEl.isConnected) {
      const el = document.createElement("div");
      el.className = "hub-detail-layer";
      // Anchored to the panel window, never to .hub-content (rewritten by
      // _renderGrid) and never to the shadow root (which would make the layer
      // cover the whole viewport including ComfyUI's own UI).
      // Parent is the body element: the layer must sit inside the body's box
      // (below the header and filter bar) and above the grid, but it must not be
      // a child of .hub-content because _renderGrid rewrites that.
      // Insert at the TOP of the body, not appended: a sticky element only pins
      // once its own position reaches the viewport, so a layer appended after the
      // grid sat at the very bottom and was invisible while the user was mid-list
      // (which also made the page appear to jump back to the top).
      const parent = this.bodyEl || this.$(".hub-body") || this.shadow;
      parent.insertBefore(el, parent.firstChild);
      // Handle clicks locally instead of relying on the overlay's delegated
      // listener: the layer is outside .hub-content, and delegation through the
      // shadow boundary proved unreliable for buttons inside it.
      // The layer is a child of the overlay's body, so without this the click also
      // bubbles to the overlay's own delegated handler and every action runs twice
      // — one 下载 click queued two identical downloads.
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this._click(ev);
      });
      this._detailEl = el;
    }
    const host = this._detailEl;
    this._showDetailView();
    const vIdx = Math.min(this.state.activeVersion, (m.versions || []).length - 1);
    const ver = (m.versions || [])[vIdx] || {};
    // Civitai's index lists a model under every base model any of its versions has
    // ever had, and authors do delete versions afterwards. ControlNetXL is a live
    // example: indexed as Pony, but its 50 versions are SDXL 1.0 and Illustrious
    // only. Filtering by Pony then cannot possibly open a Pony version, so the
    // pane explains that instead of quietly showing something else.
    const basesHere = new Set((m.versions || []).map((v) => v.baseModel).filter(Boolean));
    const filterMissing = !!this.state.baseModel && !basesHere.has(this.state.baseModel);
    // Sort videos out first. A model whose examples are all clips is common, and
    // those clips carry a high nsfwLevel, so counting them as explicit images
    // would report "explicit content hidden" for a model that simply has no
    // stills at all.
    const rawImgs = ver.images || [];
    const isClip = (u) => {
      const low = String(u || "").toLowerCase().split("?")[0];
      return low.endsWith((".mp4", ".webm", ".mov"));
    };
    const stills = rawImgs.filter((im) => im && im.url && !isClip(im.url));
    const videoOnly = rawImgs.length > 0 && stills.length === 0;
    const explicitHidden = stills.some((im) => im.nsfw);
    const images = stills.filter((im) => this.state.nsfw || !im.nsfw);
    // A version can genuinely ship no stills at all — a brand new base model, or
    // clips only — while its siblings are full of pictures. Borrowing them is
    // useful; borrowing them *silently* is not: it made an Anima-filtered card
    // open on what looked like an Illustrious model, which reads as a broken
    // filter. Track the source so the pane can say whose examples these are.
    let borrowed = null;
    if (!images.length) {
      const siblings = (m.versions || [])
        .map((v, i) => ({ v, i }))
        .filter(({ v, i }) => i !== vIdx
          && (v.images || []).some((im) => im && im.url && !isClip(im.url)));
      // Same base model first, then the same base-model family (Anima derived from
      // Illustrious, SD 1.5 vs SD 2.1, ...), and only then any sibling at all.
      const pick = siblings.find(({ v }) => this.state.baseModel && v.baseModel === this.state.baseModel)
        || siblings.find(({ v }) => ver.baseModelType && v.baseModelType === ver.baseModelType)
        || siblings[0];
      if (pick) {
        const pool = (pick.v.images || []).filter((im) => im && im.url && !isClip(im.url));
        const take = this.state.nsfw ? pool : pool.filter((im) => !im.nsfw);
        if (take.length) {
          images.push(...take.slice(0, 12));
          borrowed = pick.v;
        }
      }
    }
    const hero = images[Math.min(this.state.heroIndex, Math.max(images.length - 1, 0))];
    const stats = m.stats || {};
    const chips = (m.versions || []).map((v, i) => {
      // Publication date per version: with 30+ versions it is the only way to
      // tell which one is current and which is an old experiment.
      const when = v.publishedAt ? String(v.publishedAt).slice(0, 10) : "";
      return `
      <div class="hub-chip${i === vIdx ? " active" : ""}" data-act="version" data-idx="${i}"
           title="${esc(v.name || "")}${when ? " · 发布于 " + when : ""}">
        ${esc(v.name || v.id)}<span class="base">${esc(v.baseModel || "")}</span>${
          when ? `<span class="when">${esc(when)}</span>` : ""}
      </div>`;
    }).join("");

    // The snapshot deliberately keeps no file list — only a hasFiles flag — so a
    // card opened from the grid has files: [] for about a second until the live
    // payload lands. Claiming "该版本没有可取的文件" in that window was simply
    // wrong, and it flashed for every card that does have files.
    const filesList = ver.files || [];
    const files = filesList.map((f, i) => `
      <div class="hub-file">
        <div class="hub-file-info">
          <div class="hub-file-name">${esc(f.name)}</div>
          <div class="hub-file-size">${humanSize(f.sizeKB)} · ${esc(f.type || "Model")}${f.primary ? " · 主文件" : ""}</div>
        </div>
        <button class="hub-btn primary" data-act="download" data-vidx="${vIdx}" data-fidx="${i}">下载</button>
      </div>`).join("")
      || (ver.hasFiles
        ? `<div class="hub-note">正在获取文件信息…</div>`
        : `<div class="hub-note">该版本没有可取的文件。</div>`);

    const early = ver.earlyAccessEndsAt
      ? `<div class="hub-note">该版本处于抢先体验期，匿名下载可能被拒。</div>` : "";

    // Trigger words live in the version payload, not the description; some
    // authors only ever write them there.
    const words = (ver.trainedWords || []).filter((w) => w && String(w).trim());
    const wordsBlock = words.length ? `
      <div class="hub-section">
        <h3>触发词 · ${words.length}</h3>
        <div class="hub-words">
          ${words.map((w) => `<div class="hub-word" data-act="copy" data-text="${esc(w)}">
            ${esc(w)}<span>点击复制</span></div>`).join("")}
        </div>
      </div>` : "";

    host.innerHTML = `
      <div style="margin-bottom:14px">
        <button class="hub-btn" data-act="back">← 返回列表</button>
      </div>
      <div class="hub-detail">
        <div class="hub-gallery">
          <div class="hub-hero" data-act="lightbox" data-idx="${this.state.heroIndex}" style="cursor:zoom-in">
            ${hero ? `<img class="hub-hero-img"
                        src="${esc(m.coverNsfwUrl || coverUrl(hero.url, 240))}"
                        data-full="${esc(m.coverNsfwUrl || thumbUrl(hero.url, 900, HERO_SEG))}"
                        data-origin="${esc(hero.url)}" alt="">`
                   : `<div style="color:#7f8ea3;font-size:12px;padding:60px;text-align:center;line-height:2">
                        <b style="color:#9aa9bd;font-size:13px">这个模型没有可显示的预览图</b><br>
                        <span style="color:#5b6675;font-size:11px">
                          ${videoOnly
                            ? "作者在 Civitai 上只上传了视频示例，还没有发布任何静态图。<br>插件不播放视频，所以这里留空。"
                            : (explicitHidden && !this.state.nsfw
                                ? "该版本的静态示例图都是露骨内容，已按「成人内容 关」隐藏。<br>想看就在顶部把成人内容打开。"
                                : "这个版本没有可供显示的示例图。")}<br><br>
                          你仍然可以下载它，或在 Civitai 网页上查看。
                        </span>
                      </div>`}
          </div>
          <div class="hub-strip">
            ${images.map((img, i) => `
              <img src="${esc(coverUrl(img.url, 240))}" data-act="hero" data-idx="${i}"
                   data-origin="${esc(img.url)}"
                   class="${i === this.state.heroIndex ? "active" : ""}" alt="">`).join("")}
          </div>
          ${borrowed ? `<div class="hub-note" style="margin-top:8px">
            当前版本「${esc(ver.name || ver.id)}」没有静态示例，上面显示的是「${esc(borrowed.name || borrowed.id)}」${borrowed.baseModel ? " · " + esc(borrowed.baseModel) : ""} 的示例图。
          </div>` : ""}
        </div>
        <div class="hub-info">
          <div>
            <h2>${esc(m.name)}</h2>
            <div class="hub-author">by ${esc(m.creator?.username || "匿名")} · ${esc(m.type || "")}</div>
          </div>
          <div class="hub-stats">
            <span>↓ ${humanCount(stats.downloadCount)}</span>
            <span>♥ ${humanCount(stats.thumbsUpCount)}</span>
            <span>💬 ${humanCount(stats.commentCount)}</span>
            <span>★ ${stats.rating ? Number(stats.rating).toFixed(2) : "-"}</span>
          </div>
          <div class="hub-card-tags">${(m.tags || []).map((t) => `<span class="hub-tag">${esc(t)}</span>`).join("")}</div>
          <div class="hub-section">
            ${filterMissing ? `<div class="hub-note" style="margin-bottom:8px">
              Civitai 的索引仍把这个模型算在「${esc(this.state.baseModel)}」里，但它现在没有任何 ${esc(this.state.baseModel)} 版本（作者删掉或换过底模）。下面显示的是 ${esc(ver.baseModel || "其它底模")} 版本。
            </div>` : ""}
            ${this.state.versionNotice ? `<div class="hub-note" style="margin-bottom:8px">${esc(this.state.versionNotice)}</div>` : ""}
            <h3>版本 ${m.versions?.length ? `(${m.versions.length})` : ""}</h3>
            <div class="hub-versions">${chips || "无版本信息"}</div>
          </div>
          ${wordsBlock}
          <div class="hub-section">
            <h3>文件</h3>
            ${files}${early}
          </div>
          <div class="hub-section">
            <h3>简介</h3>
            <div class="hub-desc">${m.description || esc(m.summary || "无简介")}</div>
          </div>
        </div>
      </div>`;
    this.bodyEl.scrollTop = 0;
    this._setMeta(ver.baseModel || "");
    this._armHero();
    this._armStrip();
  }

  _renderSettings() {
    this.state.view = "settings";
    const c = this.state.config || {};
    this.content.innerHTML = `
      <div style="margin-bottom:14px"><button class="hub-btn" data-act="back">← 返回列表</button></div>
      <div class="hub-settings">
        <div class="hub-field">
          <label>Civitai API Key <span style="color:#e5534b">*下载必需</span></label>
          <input type="text" data-cfg="civitai_api_key"
                 value="${esc(c.civitai_api_key || "")}"
                 placeholder="粘贴你的 API Key"
                 spellcheck="false" autocomplete="off">
          <small>不填只能浏览，不能下载模型文件。获取方法见下方说明。</small>
          <small style="color:#7c9ef5">
            🔒 这个 Key 只保存在你本机的 <code>config.json</code> 里，不会写进插件代码，
            也不会发送到除 Civitai 之外的任何地方。分享插件前把这个文件删掉即可。
          </small>
        </div>

        <details class="hub-help">
          <summary>怎么获取 API Key（点开）</summary>
          <ol>
            <li>浏览器打开 <b>civitai.com</b> 并登录（需要能访问，或用你自己的方式打开）。</li>
            <li>点右上角头像 → <b>Account settings</b>（账户设置）。</li>
            <li>左侧选 <b>API Keys</b>，再点 <b>Add API key</b>。</li>
            <li>名字随便填（例如 comfyui），生成后复制那串 <b>32 位字符</b>。</li>
            <li>粘贴到上面的输入框，点底部「保存设置」。</li>
          </ol>
          <p>Key 只在本机 <code>config.json</code> 里保存，不会发到除 Civitai 之外的地方。</p>
        </details>

        <div class="hub-field">
          <label>各类模型的保存目录</label>
          <div class="hub-dirs">
            ${TYPES.filter(([v]) => v !== "All").map(([v, label]) => `
              <div class="hub-dir-row">
                <span>${esc(label)}</span>
                <input type="text" data-cfg-dir="${esc(v)}"
                       value="${esc((c.save_dirs || {})[v] || "")}"
                       placeholder="loras">
              </div>`).join("")}
          </div>
          <small>
            每个类型单独指定存到 <code>models/</code> 下的哪个子目录。留空则回退到 <code>loras</code>。<br>
            例如 LoRA 填 <code>loras</code>、Checkpoint 填 <code>checkpoints</code>，各下各的不会混在一起。
          </small>
        </div>

        <div class="hub-field">
          <label>并发下载段数</label>
          <input type="number" min="1" max="128" data-cfg="download_segments"
                 value="${esc(c.download_segments || 64)}">
          <small>单个文件切成几段并行拉。默认 64，实测约 5.8MB/s；网络不稳可调到 32。</small>
        </div>

        <details class="hub-help">
          <summary>高级：连接通道（通常不用改）</summary>

          <div class="hub-field">
            <label>Civitai API 镜像</label>
            <input type="text" data-cfg="api_mirrors" value="${esc((c.api_mirrors || []).join(", "))}">
            <small>
              <b>作用：</b>civitai.com 在国内直连超时，这里填的镜像负责转发所有接口请求（搜索、模型详情、版本信息）。<br>
              <b>默认：</b><code>civitai.red, civitai.work</code>。两个是同一后端，插件会同时竞速、自动用快的那个，坏的会暂停一段时间再重试。<br>
              <b>什么时候改：</b>只有这两个都挂了你才需要换。填任意能访问 <code>/api/v1</code> 的反代地址即可，逗号分隔。
            </small>
          </div>

          <div class="hub-field">
            <label>图片代理</label>
            <input type="text" data-cfg="image_proxies" value="${esc((c.image_proxies || []).join(", "))}">
            <small>
              <b>作用：</b>所有预览图都存在 image.civitai.com，该域名被墙，必须经图片代理转发才能显示。<br>
              <b>默认：</b><code>wsrv.nl</code>。它会把图压到 240px 再返回，比原图快得多；下载过的图会存进本地磁盘缓存，第二次直接读盘。<br>
              <b>什么时候改：</b>wsrv 挂了才需要换。填任意支持 <code>?url=</code> 参数的公益图片代理。
            </small>
          </div>
        </details>

        <div style="display:flex;gap:10px;align-items:center">
          <button class="hub-btn primary" data-act="save-config">保存设置</button>
          <button class="hub-btn" data-act="health">检测通道</button>
          <button class="hub-btn" data-act="reset-dirs" title="把所有目录恢复为默认值">重置目录</button>
          <div class="hub-health"></div>
        </div>
      </div>`;
    this._setMeta("设置");
  }

  async _renderLocal() {
    this.state.view = "local";
    this._loading(true);
    try {
      const data = await jget(`${BASE}/local?type=${encodeURIComponent(this.state.types === "All" ? "LORA" : this.state.types)}`);
      const rows = (data.files || []).map((f) => `
        <div class="hub-file">
          <div class="hub-file-info">
            <div class="hub-file-name">${esc(f.name)}</div>
            <div class="hub-file-size">${humanSize(f.size / 1024)}</div>
          </div>
        </div>`).join("") || `<div class="hub-note">目录为空。</div>`;
      this.content.innerHTML = `
        <div style="margin-bottom:14px"><button class="hub-btn" data-act="back">← 返回列表</button></div>
        <div class="hub-settings" style="max-width:100%">
          <div class="hub-field">
            <label>本地目录 · ${esc(data.folder)}</label>
            <div class="hub-file-name" style="white-space:normal">${esc(data.path)}</div>
          </div>
          ${rows}
        </div>`;
      this._setMeta(`${(data.files || []).length} 个文件`);
    } catch (err) {
      this._renderEmpty("读取本地目录失败", String(err.message || err));
    } finally {
      this._loading(false);
    }
  }

  async _saveConfig() {
    const patch = {};
    this.content.querySelectorAll("[data-cfg]").forEach((el) => {
      const key = el.dataset.cfg;
      let value = el.value;
      if (["api_mirrors", "image_proxies", "b2_endpoints"].includes(key)) {
        value = value.split(/[,\s]+/).map((v) => v.trim()).filter(Boolean);
      } else if (["max_concurrent_downloads", "thumb_width", "download_segments"].includes(key)) {
        // Input values are always strings. Saving without converting wrote
        // download_segments back as "64" and left a numeric option typed as text
        // in config.json.
        value = Number(value) || undefined;
      }
      if (key === "civitai_api_key" && !value) return;
      patch[key] = value;
    });

    // Per-type destination folders, gathered separately because they are one
    // map rather than individual top-level keys.
    const dirs = {};
    this.content.querySelectorAll("[data-cfg-dir]").forEach((el) => {
      const t = el.dataset.cfgDir;
      const v = el.value.trim();
      if (t) dirs[t] = v;
    });
    if (Object.keys(dirs).length) patch.save_dirs = dirs;

    const accounts = {};
    this.content.querySelectorAll("[data-acc]").forEach((el) => {
      const [site, field] = el.dataset.acc.split(".");
      const value = el.value.trim();
      accounts[site] = accounts[site] || {};
      accounts[site][field] = value;
    });
    if (Object.keys(accounts).length) patch.accounts = accounts;

    try {
      this.state.config = await jpost(`${BASE}/config`, patch);
      this._toast("设置已保存");
      this._renderSettings();
      this.loadSites();
    } catch (err) {
      this._toast(`保存失败：${err.message || err}`, true);
    }
  }

  async _testSite(site) {
    const box = this.content.querySelector(`[data-test="${site}"]`);
    if (box) box.innerHTML = `<span>测试中…</span>`;
    try {
      const data = await jpost(`${BASE}/site_test`, { site });
      if (!box) return;
      if (data.ok) {
        box.innerHTML = `<span class="ok">接通 · ${data.count} 条 · ${(data.sample || []).join(" / ")}</span>`;
      } else {
        box.innerHTML = `<span class="bad">${esc(data.error || "失败")}</span>`;
      }
    } catch (err) {
      if (box) box.innerHTML = `<span class="bad">${esc(String(err.message || err))}</span>`;
    }
  }

  async _health() {
    const box = this.content.querySelector(".hub-health");
    if (box) box.innerHTML = `<span>检测中…</span>`;
    try {
      const data = await jget(`${BASE}/health`);
      const rows = [];
      for (const [host, info] of Object.entries(data.mirrors || {})) {
        rows.push(`<span class="${info.ok ? "ok" : "bad"}">API ${esc(host)} ${info.ok ? `${info.ms}ms` : "失败"}</span>`);
      }
      for (const [host, info] of Object.entries(data.images || {})) {
        rows.push(`<span class="${info.ok ? "ok" : "bad"}">图 ${esc(host)} ${info.ok ? `${info.ms}ms` : "失败"}</span>`);
      }
      if (box) box.innerHTML = rows.join("");
    } catch (err) {
      if (box) box.innerHTML = `<span class="bad">${esc(String(err.message || err))}</span>`;
    }
  }

  async _download(vidx, fidx) {
    const m = this.state.model;
    if (!m) return;
    const ver = (m.versions || [])[vidx];
    if (!ver) return;
    const file = (ver.files || [])[fidx];
    if (!file) return;
    const payload = {
      model_id: m.id,
      model_name: m.name,
      version_id: ver.id,
      version_name: ver.name,
      model_type: m.type,
      base_model: ver.baseModel,
      preview_url: (ver.images || [])[0]?.url || "",
      file_name: file.name,
      sizeKB: file.sizeKB,
      sha256: file.sha256,
      files: ver.files || [],
    };
    try {
      await jpost(`${BASE}/download`, payload);
      this.dock.classList.add("open");
      this._toast(`已开始下载 ${file.name}`);
      this._poll();
    } catch (err) {
      this._toast(`下载启动失败：${err.message || err}`, true);
    }
  }

  async _poll() {
    if (this._pollTimer) { clearTimeout(this._pollTimer); this._pollTimer = null; }
    if (!this.overlay.classList.contains("open") && !this.dock.classList.contains("open")) {
      return;
    }
    try {
      const data = await jget(`${BASE}/downloads`);
      this._renderTasks(data.tasks || []);
    } catch { /* ignore transient errors */ }
    this._pollTimer = setTimeout(() => this._poll(), 1500);
  }

  _renderTasks(tasks) {
    const active = tasks.filter((t) => t.status === "downloading" || t.status === "queued").length;
    if (!tasks.length) {
      this.dockBody.innerHTML = `<div class="hub-empty" style="padding:22px 8px">暂无下载任务</div>`;
      return;
    }
    this.dockBody.innerHTML = tasks.map((t) => {
      const pct = t.total ? t.percent : 0;
      const speed = t.speed > 0 ? `${(t.speed / 1024 / 1024).toFixed(2)} MB/s` : "";
      const eta = t.eta ? `剩余 ${Math.ceil(t.eta)}s` : "";
      const statusText = {
        queued: "排队中",
        downloading: `${humanSize(t.downloaded / 1024)} / ${humanSize(t.total / 1024)}`,
        completed: `完成 · ${humanSize(t.total / 1024)}`,
        failed: t.error || "失败",
        cancelled: "已取消",
      }[t.status] || t.status;
      const cls = t.status === "failed" ? "err" : t.status === "completed" ? "ok" : "";
      return `
        <div class="hub-task">
          <div class="hub-task-head">
            <div class="hub-task-name" title="${esc(t.file_name)}">${esc(t.file_name)}</div>
            <div class="hub-task-pct">${t.status === "downloading" ? pct.toFixed(1) + "%" : ""}</div>
            ${t.status === "downloading" || t.status === "queued"
              ? `<button class="hub-btn ghost" data-act="cancel" data-id="${t.id}" style="padding:1px 7px;font-size:10.5px">停止</button>` : ""}
          </div>
          <div class="hub-bar"><i style="width:${pct}%"></i></div>
          <div class="hub-task-sub">
            <span class="${cls}">${esc(statusText)}</span>
            <span>${esc(speed)} ${esc(eta)}</span>
          </div>
        </div>`;
    }).join("");
    if (active > 0) this.dock.classList.add("open");
  }

  _click(ev) {
    const el = ev.target.closest("[data-act]");
    if (!el) {
      const card = ev.target.closest(".hub-card");
      if (card) this.openDetail(card.dataset.id, Number(card.dataset.idx));
      return;
    }
    const act = el.dataset.act;
    switch (act) {
      case "close": this.close(); break;
      case "search":
        this.state.query = this.searchInput.value.trim();
        this.state.page = 1;
        this.loadGrid();
        break;
      case "more":
        if (!this.state.nextCursor) this.state.page += 1;
        this.loadGrid(true);
        break;
      case "back": {
        // Two different returns land here:
        //  - from the detail layer: the grid is still in the DOM, just hidden, so
        //    unhide it and the scroll offset is untouched;
        //  - from settings/local: those views replaced the content, so the grid
        //    has to be rebuilt and the saved offset reapplied.
        // The layer lives under .hub-body, not .hub-content, so querying content
        // always returned null and every return took the rebuild path — which is
        // exactly what discarded the scroll position.
        const hasLayer = !!this._detailEl && this._detailEl.isConnected;
        const wasDetail = !!(this._detailEl && this._detailEl.classList.contains("open"));
        // Always close the layer, whichever branch runs: leaving it open in the
        // rebuild path made every later view (settings, local) render behind a
        // model page that looked stuck.
        this._showGridView();
        this.state.view = "grid";
        this.state.model = null;
        if (wasDetail && hasLayer) {
          // The grid DOM was never rebuilt, but hiding it collapses the body's
          // scrollable height to the detail layer's own height, and the browser
          // clamps scrollTop to that. Returning from a short model page after
          // scrolling deep therefore landed at the top: measured 3162 -> 0.
          // Re-apply the offset explicitly instead of trusting it to survive.
          const keep = this._listScroll || 0;
          if (keep > 0 && this.bodyEl) {
            let tries = 0;
            const restore = () => {
              if (!this.bodyEl || this.state.view !== "grid") return;
              this.bodyEl.scrollTop = keep;
              tries += 1;
              if (tries < 24 && Math.abs(this.bodyEl.scrollTop - keep) > 2) {
                requestAnimationFrame(restore);
              }
            };
            requestAnimationFrame(restore);
          }
        } else {
          const keep = this._listScroll || 0;
          this._renderGrid(false);
          if (keep > 0 && this.bodyEl) {
            let tries = 0;
            const restore = () => {
              if (!this.bodyEl || this.state.view !== "grid") return;
              this.bodyEl.scrollTop = keep;
              tries += 1;
              if (tries < 24 && Math.abs(this.bodyEl.scrollTop - keep) > 2) {
                requestAnimationFrame(restore);
              }
            };
            requestAnimationFrame(restore);
          }
        }
        // Detail previews are NOT released here: coming back to the same model
        // should show its images instantly. They are cleaned when the filter
        // changes or when the panel is opened next time.
        break;
      }
      case "settings":
        // Remember the list position before settings wipes the content.
        if (this.bodyEl) this._listScroll = this.bodyEl.scrollTop;
        // The detail layer floats above the content; leaving it open made the
        // settings page appear behind the previously opened model.
        this._showGridView();
        this._toTop();
        this.loadConfig().then(() => this._renderSettings());
        break;
      case "local":
        if (this.bodyEl) this._listScroll = this.bodyEl.scrollTop;
        this._showGridView();
        this._toTop();
        this._renderLocal();
        break;
      case "save-config": this._saveConfig(); break;
      case "reset-dirs": {
        // Restore every row to its built-in default in one click instead of
        // clearing fourteen inputs by hand.
        const rows = this.content.querySelectorAll("[data-cfg-dir]");
        rows.forEach((el) => {
          el.value = DEFAULT_DIRS[el.dataset.cfgDir] || "";
        });
        this._toast("目录已重置为默认值，记得点保存");
        break;
      }
      case "health": this._health(); break;
      case "test-site": this._testSite(el.dataset.site); break;
      case "version":
        this.state.activeVersion = Number(el.dataset.idx);
        this.state.heroIndex = 0;
        // The user picked this version, so the "that one is gone, switched for
        // you" notice has done its job.
        this.state.versionNotice = "";
        this._renderDetail();
        // Pull this version's own screenshots; the snapshot only holds the
        // primary version's, which is why other versions showed a bare strip.
        this._loadVersionImages(this.state.model.versions?.[this.state.activeVersion]?.id);
        break;
      case "hero": {
        this.state.heroIndex = Number(el.dataset.idx);
        const ver = this.state.model.versions[this.state.activeVersion];
        const heroEl = this._detailRoot().querySelector(".hub-hero");
        const img = ver.images[this.state.heroIndex];
        if (heroEl && img) {
          // Keep data-idx in sync: the lightbox reads it, so leaving it at the
          // initial value made every later thumbnail open the first image.
          heroEl.dataset.idx = String(this.state.heroIndex);
          // Bundled 240px first so the frame is never blank, then upgrade in the
          // background; the large proxy often stalls and used to leave it empty.
          const small = coverUrl(img.url, 240);
          const big = thumbUrl(img.url, 900, HERO_SEG);
          heroEl.innerHTML = `<img src="${esc(small)}" data-full="${esc(big)}" alt="">`;
          const node = heroEl.querySelector("img");
          if (node && big !== small) {
            const hi = new Image();
            hi.onload = () => { node.src = big; };
            hi.src = big;
          }
          heroEl.classList.toggle("nsfw", !!img.nsfw);
          heroEl.classList.remove("revealed");
        }
        this._detailRoot().querySelectorAll('.hub-strip img').forEach((n) => n.classList.remove("active"));
        el.classList.add("active");
        break;
      }
      case "reveal":
        el.classList.toggle("revealed");
        break;
      case "lightbox":
        this._openLightbox(Number(el.dataset.idx) || 0);
        break;
      case "copy":
        navigator.clipboard?.writeText(el.dataset.text || "");
        this._toast("已复制触发词");
        break;
      case "download":
        this._download(Number(el.dataset.vidx), Number(el.dataset.fidx));
        break;
      case "cancel":
        jpost(`${BASE}/downloads/${el.dataset.id}/cancel`, {}).catch(() => {});
        break;
      case "clear":
        jpost(`${BASE}/downloads/clear`, {}).then(() => this._poll()).catch(() => {});
        break;
      case "dock-close": this.dock.classList.remove("open"); break;
      default: break;
    }
  }

  async _openLightbox(idx) {
    // Only one lightbox at a time: opening twice (double click, or a stale box
    // left behind) made the close button appear to need two presses.
    this.shadow.querySelectorAll(".hub-lightbox").forEach((n) => n.remove());
    const model = this.state.model;
    if (!model) return;
    const ver = (model.versions || [])[this.state.activeVersion] || {};
    const img = (ver.images || [])[idx];
    if (!img) return;

    const box = document.createElement("div");
    box.className = "hub-lightbox";
    box.innerHTML = `
      <button class="hub-close" data-lb="close">✕</button>      <div class="hub-lb-img"><img src="${esc(thumbUrl(img.url, 900, HERO_SEG))}"
           data-origin="${esc(img.url)}" alt=""></div>
      <div class="hub-lb-info">
        <div>
          <h4>图片信息</h4>
          <div class="hub-kv"><b>尺寸</b><span>${img.width || "?"} × ${img.height || "?"}</span></div>
          <div class="hub-kv"><b>所属版本</b><span>${esc(ver.name || "-")}</span></div>
          <div class="hub-kv"><b>底模</b><span>${esc(ver.baseModel || "-")}</span></div>
          <div class="hub-kv"><b>分级</b><span>${img.nsfw ? "NSFW" : "SFW"}</span></div>
        </div>
        <div>
          <h4>生成参数</h4>
          <div data-lb="meta" style="color:#6e7b8b;font-size:11.5px">读取中…</div>
        </div>
      </div>`;
    box.addEventListener("click", (ev) => {
      // Stop here: the overlay's delegated handler also listens for clicks and
      // was re-processing the same event, so the first click merely removed the
      // box while a second was needed to settle the panel state.
      ev.stopPropagation();
      const target = ev.target.closest("[data-lb]");
      if (target) {
        if (target.dataset.lb === "close") {
          box.remove();
          return;
        }
        if (target.dataset.lb === "copy" || target.dataset.lb === "meta") {
          const text = target.dataset.copy;
          if (text) {
            navigator.clipboard?.writeText(text);
            this._toast("已复制");
          }
        }
        return;
      }
      // Click anywhere outside the image/info panes closes it.
      if (ev.target === box || ev.target.classList?.contains("hub-lb-img")) {
        box.remove();
      }
    });
    const onKey = (ev) => {
      if (ev.key === "Escape") {
        box.remove();
        document.removeEventListener("keydown", onKey);
      }
    };
    document.addEventListener("keydown", onKey);
    box.addEventListener("remove", () => document.removeEventListener("keydown", onKey));
    this.shadow.appendChild(box);

    // 2000px in one proxied request took many seconds. Ask for 900px with ranged
    // fetching instead; a blurred 240px upscale looked worse than waiting, so the
    // placeholder is not used here.
    const lbImg = box.querySelector(".hub-lb-img img");
    if (lbImg) {
      lbImg.style.opacity = "0";
      lbImg.style.transition = "opacity .18s ease";
      const reveal = () => { lbImg.style.opacity = "1"; };
      if (lbImg.complete && lbImg.naturalWidth) reveal();
      else lbImg.addEventListener("load", reveal, { once: true });
    }

    // Images from /version_detail carry no id/hash (v1 omits both), so bailing
    // out here made most pictures report "no parameters". The url is always
    // present and the backend matches on it.
    if (!img.id && !img.hash && !img.url) {
      const el = box.querySelector('[data-lb="meta"]');
      if (el) el.textContent = "该图片没有可用的生成参数。";
      return;
    }
    const metaEl = box.querySelector('[data-lb="meta"]');
    try {
      // Always pass the version too. Relying on image_id alone hit
      // /api/v1/images, which strips meta for anonymous callers and returns the
      // wrong picture, so every thumbnail showed the first image's parameters.
      const parts = [];
      if (ver?.id) parts.push(`version_id=${encodeURIComponent(ver.id)}`);
      if (img.id) parts.push(`image_id=${encodeURIComponent(img.id)}`);
      if (img.hash) parts.push(`hash=${encodeURIComponent(img.hash)}`);
      if (img.url) parts.push(`url=${encodeURIComponent(img.url)}`);
      const q = parts.join("&");
      const data = await jget(`${BASE}/image_meta?${q}`);
      if (!metaEl) return;
      const meta = data.meta;
      if (!meta || !Object.keys(meta).length) {
        // Keep this quiet: a wall of text about API keys makes the panel feel
        // broken when it is simply Civitai policy.
        metaEl.innerHTML = `<span style="color:#5b6675">这张图没有可读的生成参数。</span>`;
        return;
      }
      const pretty = Object.entries(meta)
        .map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`)
        .join("\n");
      metaEl.className = "hub-param";
      metaEl.innerHTML = this._metaHtml(meta, ver);
      metaEl.dataset.copy = pretty;
      metaEl.dataset.lb = "meta";
      metaEl.title = "点击复制全部参数";
    } catch (err) {
      if (metaEl) metaEl.textContent = `读取失败：${err.message || err}`;
    }
  }

  // Mirrors how Civitai groups generation data: prompts first, then the
  // sampling knobs, then whatever extra resources the image mentions.
  _metaHtml(meta, ver) {
    const block = (title, text, key) => {
      if (!text) return "";
      return `
        <div class="hub-meta-block">
          <div class="hub-meta-head">${esc(title)}
            <button class="hub-mini" data-lb="copy" data-copy="${esc(text)}">复制</button>
          </div>
          <pre class="hub-meta-text">${esc(text)}</pre>
        </div>`;
    };
    const rows = (pairs) => {
      const filled = pairs.filter(([, v]) => v !== undefined && v !== null && v !== "");
      if (!filled.length) return "";
      return `<div class="hub-meta-grid">${filled.map(([k, v]) =>
        `<div class="hub-kv"><b>${esc(k)}</b><span>${esc(v)}</span></div>`).join("")}</div>`;
    };

    let html = "";
    html += block("正向提示词", meta.prompt, "prompt");
    // Always render the negative block: hiding it made the pane look broken when
    // the field was simply empty.
    html += meta.negativePrompt
      ? block("负向提示词", meta.negativePrompt, "negativePrompt")
      : `<div class="hub-meta-block"><div class="hub-meta-head">负向提示词</div>
           <pre class="hub-meta-text hub-meta-empty">（这张图没有负向提示词）</pre></div>`;

    html += rows([
      ["采样器", meta.sampler],
      ["调度器", meta.scheduler],
      ["步数", meta.steps],
      ["CFG", meta.cfgScale],
      ["种子", meta.seed],
      ["Clip Skip", meta.clipSkip],
      ["尺寸", meta.Size || meta.size],
      ["底模", ver?.baseModel],
    ]);

    const res = (meta.resources || []).filter((r) => r && r.name);
    const loras = res.filter((r) => (r.type || "").toLowerCase() === "lora");
    const others = res.filter((r) => (r.type || "").toLowerCase() !== "lora"
      && (r.type || "").toLowerCase() !== "model");

    if (loras.length) {
      html += `<div class="hub-meta-head">使用的 LoRA
        <button class="hub-mini" data-lb="copy" data-copy="${
          esc(loras.map((r) => `<lora:${r.name}:${r.weight ?? 1}>`).join(" "))
        }">复制全部</button></div>`
        + `<div class="hub-meta-tags">${loras.map((r) =>
          `<span class="hub-tag">${esc(r.name)}${r.weight != null ? " ×" + r.weight : ""}</span>`
        ).join("")}</div>`;
    }
    if (others.length) {
      html += `<div class="hub-meta-head">其他资源</div>`
        + `<div class="hub-meta-tags">${others.map((r) =>
          `<span class="hub-tag">${esc(r.type || "?")}: ${esc(r.name)}</span>`
        ).join("")}</div>`;
    }

    // Only short scalar leftovers. Nested objects (civitai / comfy / hashes)
    // used to be stringified inline, which turned the panel into one long
    // unreadable line of JSON.
    const skip = ["prompt", "negativePrompt", "sampler", "scheduler", "steps",
      "cfgScale", "seed", "clipSkip", "Size", "size", "resources", "hashes",
      "civitai", "comfy", "Model", "VAE", "Lora"];
    const leftovers = Object.entries(meta).filter(([k, v]) => {
      if (skip.includes(k)) return false;
      if (v === null || v === undefined) return false;
      if (typeof v === "object") return false;
      return String(v).length <= 120;
    });
    if (leftovers.length) {
      html += `<div class="hub-meta-head">其他参数</div>` + rows(leftovers);
    }
    return html || `<span style="color:#5b6675">这张图没有可读的生成参数。</span>`;
  }

  _toast(message, isError) {
    const el = document.createElement("div");
    el.textContent = message;
    el.style.cssText = `
      position:fixed;left:50%;bottom:128px;transform:translateX(-50%);
      background:${isError ? "#4a1d20" : "#132e1c"};color:${isError ? "#ff9c9c" : "#8fe3a6"};
      border:1px solid ${isError ? "#7a2b30" : "#285c37"};border-radius:8px;
      padding:9px 16px;font:600 12px system-ui,"Microsoft YaHei",sans-serif;z-index:1000001;
      box-shadow:0 8px 24px rgba(0,0,0,.5);max-width:70vw;pointer-events:none;`;
    this.shadow.appendChild(el);
    setTimeout(() => el.remove(), isError ? 5200 : 2600);
  }
}

let panel = null;

app.registerExtension({
  name: EXT_NAME,
  async setup() {
    if (panel) return;
    panel = new HubPanel();
    window.__civitaiHub = panel;
    window.__hubDiag = () => panel._diag();
    // Fire-and-forget: hits the backend the moment ComfyUI's page loads, which
    // is minutes before anyone opens the panel. That call starts the adult-pool
    // warm-up, so the first real search does not pay for it.
    try {
      api.fetchApi(`${BASE}/ping`, { cache: "no-store" }).catch(() => {});
    } catch (_) { /* non-fatal */ }
    console.log("[CivitaiHub] panel mounted");
    // Prefetch the default list while the ComfyUI page is loading, so opening
    // the panel becomes a paint instead of a network round trip. The bundled
    // cover index is awaited first: rendering before it resolves sent all 291
    // pre-installed covers back through the proxy.
    setTimeout(() => {
      loadBundledIndex()
        .catch(() => {})
        .then(() => panel.loadConfig())
        .then(() => panel.loadSites())
        .then(() => panel.loadBaseModels())
        .then(() => (panel.state.items.length ? null : panel.loadGrid()))
        .catch(() => {})
        .finally(() => { panel.state.prefetched = true; });
    }, 0);
  },
});

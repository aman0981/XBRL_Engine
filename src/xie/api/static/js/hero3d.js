/* XBRL Intelligence Engine — landing hero WebGL background.
   Loaded ONLY by index.html, and ONLY after the inline gate stack passes
   (not reduced-motion/-transparency, pointer:fine, deviceMemory>=4, not
   save-data/slow, WebGL available). Never blocks LCP: the <h1> text is the
   LCP element; this canvas is absolute, starts opacity:0, and fades in.
   Raw WebGL fragment-shader aurora — no library. Palette-tinted from tokens. */
(function () {
  "use strict";
  function boot() {

  var canvas = document.getElementById("hero-canvas");
  if (!canvas) return;

  var gl = canvas.getContext("webgl", { alpha: true, antialias: false, premultipliedAlpha: false, powerPreference: "low-power" })
        || canvas.getContext("experimental-webgl");
  if (!gl) return;

  // ---- palette from CSS tokens (so the hero tracks the Aurora theme) ----
  function tokenRGB(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      var m = v.match(/^#?([0-9a-f]{6})$/i);
      if (!m) return fallback;
      var h = m[1];
      return [parseInt(h.slice(0, 2), 16) / 255, parseInt(h.slice(2, 4), 16) / 255, parseInt(h.slice(4, 6), 16) / 255];
    } catch (e) { return fallback; }
  }
  var C1 = tokenRGB("--accent", [0.31, 0.27, 0.90]);       // indigo
  var C2 = tokenRGB("--brand-glow", [0.55, 0.36, 0.96]);   // violet
  var C3 = tokenRGB("--accent-text", [0.65, 0.71, 0.99]);  // light indigo highlight

  var VERT = "attribute vec2 p;void main(){gl_Position=vec4(p,0.0,1.0);}";
  var FRAG = [
    "precision mediump float;",
    "uniform vec2 u_res;uniform float u_t;uniform vec3 c1;uniform vec3 c2;uniform vec3 c3;",
    "float hash(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}",
    "float noise(vec2 p){vec2 i=floor(p);vec2 f=fract(p);vec2 u=f*f*(3.0-2.0*f);",
    "return mix(mix(hash(i),hash(i+vec2(1.0,0.0)),u.x),mix(hash(i+vec2(0.0,1.0)),hash(i+vec2(1.0,1.0)),u.x),u.y);}",
    "float fbm(vec2 p){float v=0.0,a=0.5;for(int i=0;i<5;i++){v+=a*noise(p);p*=2.02;a*=0.5;}return v;}",
    "void main(){",
    " vec2 uv=gl_FragCoord.xy/u_res.xy;",
    " vec2 q=vec2(uv.x*(u_res.x/u_res.y),uv.y);",
    " float t=u_t*0.04;",
    " vec2 w=vec2(fbm(q*2.2+vec2(t,0.0)),fbm(q*2.2+vec2(5.2,-t)));", // domain warp
    " float n=fbm(q*3.0+w*1.6+vec2(0.0,t*1.5));",
    " float bands=smoothstep(0.35,0.95,n);",
    " float hi=pow(smoothstep(0.55,1.0,n),2.0);",
    " vec3 col=mix(c1,c2,smoothstep(0.2,0.9,n));",
    " col=mix(col,c3,hi*0.6);",
    " float topglow=smoothstep(0.0,1.1,uv.y);",          // stronger toward top
    " float intensity=bands*(0.30+0.55*topglow);",
    " float vignette=smoothstep(1.25,0.2,length(uv-vec2(0.5,0.62)));",
    " intensity*=vignette;",
    " gl_FragColor=vec4(col*intensity,clamp(intensity*1.15,0.0,0.92));",
    "}"
  ].join("\n");

  function shader(type, src) {
    var s = gl.createShader(type);
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) { return null; }
    return s;
  }
  var vs = shader(gl.VERTEX_SHADER, VERT), fs = shader(gl.FRAGMENT_SHADER, FRAG);
  if (!vs || !fs) return;
  var prog = gl.createProgram();
  gl.attachShader(prog, vs); gl.attachShader(prog, fs); gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return;
  gl.useProgram(prog);

  var buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW); // full-screen triangle
  var loc = gl.getAttribLocation(prog, "p");
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

  var uRes = gl.getUniformLocation(prog, "u_res");
  var uT = gl.getUniformLocation(prog, "u_t");
  gl.uniform3fv(gl.getUniformLocation(prog, "c1"), C1);
  gl.uniform3fv(gl.getUniformLocation(prog, "c2"), C2);
  gl.uniform3fv(gl.getUniformLocation(prog, "c3"), C3);

  var DPR = Math.min(window.devicePixelRatio || 1, 1.5);
  function resize() {
    var w = Math.max(1, Math.floor(canvas.clientWidth * DPR));
    var h = Math.max(1, Math.floor(canvas.clientHeight * DPR));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w; canvas.height = h;
      gl.viewport(0, 0, w, h);
    }
    gl.uniform2f(uRes, canvas.width, canvas.height);
  }

  // ---- lifecycle: throttled rAF, paused off-screen / hidden ----
  var running = false, visible = true, onScreen = true, raf = 0, last = 0, t0 = 0;
  var FRAME = 1000 / 40; // ~40fps cap
  function frame(now) {
    if (!running) return;
    raf = requestAnimationFrame(frame);
    if (now - last < FRAME) return;
    last = now;
    if (!t0) t0 = now;
    resize();
    gl.uniform1f(uT, (now - t0) / 1000);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
  function start() { if (running || !visible || !onScreen) return; running = true; raf = requestAnimationFrame(frame); }
  function stop() { running = false; if (raf) cancelAnimationFrame(raf); raf = 0; }

  document.addEventListener("visibilitychange", function () {
    visible = !document.hidden; if (visible) start(); else stop();
  });
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(function (es) {
      onScreen = es[0].isIntersecting; if (onScreen) start(); else stop();
    }, { threshold: 0 }).observe(canvas);
  }
  gl.getExtension("WEBGL_lose_context"); // best-effort; ignore

  function go() {
    resize();
    canvas.classList.add("ready"); // fade in (CSS)
    start();
  }
  if ("requestIdleCallback" in window) requestIdleCallback(go, { timeout: 600 });
  else setTimeout(go, 200);

  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();

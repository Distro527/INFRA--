import os
import sys
import glob
import re
import json
from datetime import timedelta
from functools import wraps
from typing import Optional

from flask import Flask, render_template, request, redirect, url_for, make_response, g, abort
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, auth as admin_auth
import bleach
from bleach.css_sanitizer import CSSSanitizer
import html as html_mod
import stripe

load_dotenv()


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")

    # Flask config
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")
    app.config["SESSION_COOKIE_NAME"] = os.getenv("SESSION_COOKIE_NAME", "vs_session")
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
    cookie_domain = os.getenv("SESSION_COOKIE_DOMAIN")
    if cookie_domain:
        app.config["SESSION_COOKIE_DOMAIN"] = cookie_domain

    # Firebase Admin init
    if not firebase_admin._apps:
        sa_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        project_id = os.getenv("FIREBASE_PROJECT_ID")
        # Fallback: try to locate a bundled service account JSON under static/
        if not sa_path or not os.path.exists(sa_path):
            app_root = os.path.dirname(os.path.abspath(__file__))
            static_dir = os.path.join(app_root, "static")
            candidates = []
            if os.path.isdir(static_dir):
                candidates = sorted(glob.glob(os.path.join(static_dir, "*firebase-adminsdk*.json")))
                # also try any .json as a last resort
                if not candidates:
                    candidates = sorted(glob.glob(os.path.join(static_dir, "*.json")))
            if candidates:
                sa_path = candidates[0]
            else:
                raise RuntimeError(
                    "Firebase service account key not found. Set GOOGLE_APPLICATION_CREDENTIALS to a valid JSON path or place a key JSON under static/."
                )
        cred = credentials.Certificate(sa_path)
        firebase_admin.initialize_app(cred, {
            'projectId': project_id
        })

    # Stripe configuration
    stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
    stripe_publishable_key = os.getenv('STRIPE_PUBLISHABLE_KEY')
    webhook_secret = os.getenv('STRIPE_WEBHOOK_SECRET')
    
    # Pro access configuration - using user registration instead of email list
    def _get_pro_users_file() -> str:
        return os.path.join(app.root_path, 'data', 'pro_users.json')
    
    def _load_pro_users() -> set:
        """Load set of Pro users from file"""
        pro_users_file = _get_pro_users_file()
        if os.path.exists(pro_users_file):
            try:
                with open(pro_users_file, 'r') as f:
                    data = json.load(f)
                    return set(data.get('pro_users', []))
            except:
                pass
        return set()
    
    def _save_pro_users(pro_users: set):
        """Save Pro users to file"""
        pro_users_file = _get_pro_users_file()
        os.makedirs(os.path.dirname(pro_users_file), exist_ok=True)
        with open(pro_users_file, 'w') as f:
            json.dump({'pro_users': list(pro_users)}, f, indent=2)
    
    PRO_USERS = _load_pro_users()  # Load existing Pro users
    
    def register_pro_user(uid: str, email: str):
        """Register a user as Pro"""
        PRO_USERS.add(uid)
        _save_pro_users(PRO_USERS)
        print(f"Registered Pro user: {email} ({uid})")
    
    def is_registered_pro_user(uid: str) -> bool:
        """Check if user is registered as Pro"""
        return uid in PRO_USERS

    # Helpers
    def _is_frozen() -> bool:
        return getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')

    def _content_dir() -> str:
        # 1) Explicit override via env var
        override = os.getenv('CONTENT_DIR')
        if override and os.path.isdir(override):
            return override
        # 2) If frozen, prefer a sibling 'content' next to the executable
        if _is_frozen():
            exe_dir = os.path.dirname(sys.executable)
            ext = os.path.join(exe_dir, 'content')
            if os.path.isdir(ext):
                return ext
        # 3) Fallback to bundled content within app root
        return os.path.join(app.root_path, 'content')
    def verify_session_cookie(req) -> Optional[dict]:
        cookie_name = app.config["SESSION_COOKIE_NAME"]
        session_cookie = req.cookies.get(cookie_name)
        if not session_cookie:
            return None
        try:
            decoded_claims = admin_auth.verify_session_cookie(session_cookie, check_revoked=True)
            return decoded_claims
        except Exception:
            return None

    def has_pro_access(user) -> bool:
        """Check if user has Pro access (paid registration or Firebase claims)"""
        if not user:
            return False
        
        uid = user.get('uid')
        if not uid:
            return False
        
        # Check if user is registered as Pro (from payment)
        is_registered_pro = is_registered_pro_user(uid)
        
        # Check if user has Firebase Pro custom claims (backup method)
        is_firebase_pro = user.get('customClaims', {}).get('pro', False)
        
        return is_registered_pro or is_firebase_pro

    def login_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = verify_session_cookie(request)
            if not user:
                return redirect(url_for('login', next=request.path))
            g.user = user
            return fn(*args, **kwargs)
        return wrapper

    def pro_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = verify_session_cookie(request)
            if not user:
                return redirect(url_for('login', next=request.path))
            
            # Check if user has Pro access (custom claim or subscription)
            is_pro = user.get('customClaims', {}).get('pro', False)
            if not is_pro:
                return redirect(url_for('pricing'))
            
            g.user = user
            return fn(*args, **kwargs)
        return wrapper

    # Routes
    # Simple in-memory lesson index
    app.config.setdefault('_LESSON_INDEX', None)

    def _slug_from_path(abs_path: str, base_dir: str) -> str:
        rel = os.path.relpath(abs_path, base_dir)
        # Normalize to forward slashes
        rel = rel.replace('\\', '/')
        if rel.lower().endswith('.json'):
            rel = rel[:-5]
        return rel

    def build_lesson_index():
        base = _content_dir()
        idx = []
        for root, _dirs, files in os.walk(base):
            for fn in files:
                if not fn.lower().endswith('.json'):
                    continue
                fp = os.path.join(root, fn)
                try:
                    with open(fp, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except Exception:
                    continue
                title = str(data.get('title') or '')
                summary = str(data.get('summary') or '')
                tags = data.get('tags') or []
                if not isinstance(tags, list):
                    tags = []
                blocks = data.get('blocks') or []
                texts = [title, summary]
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    t = (b.get('type') or '').lower()
                    if t in ('heading', 'paragraph'):
                        texts.append(str(b.get('text') or ''))
                    elif t == 'list':
                        for it in (b.get('items') or []):
                            texts.append(str(it or ''))
                    elif t == 'code':
                        texts.append(str(b.get('code') or ''))
                    elif t == 'table':
                        for h in (b.get('headers') or []): texts.append(str(h or ''))
                        for row in (b.get('rows') or []):
                            for c in (row or []): texts.append(str(c or ''))
                    elif t == 'steps':
                        for it in (b.get('items') or []): texts.append(str(it or ''))
                    elif t == 'callout':
                        texts.append(str(b.get('title') or ''))
                        texts.append(str(b.get('text') or ''))
                    elif t == 'quiz':
                        texts.append(str(b.get('question') or ''))
                        for ch in (b.get('choices') or []): texts.append(str(ch or ''))
                        texts.append(str(b.get('explanation') or ''))
                    elif t == 'link':
                        texts.append(str(b.get('text') or ''))
                    elif t == 'image':
                        texts.append(str(b.get('alt') or ''))
                slug = _slug_from_path(fp, base)
                idx.append({
                    'slug': slug,
                    'title': title,
                    'summary': summary,
                    'tags': tags,
                    'text': '\n'.join(texts).lower(),
                })
        app.config['_LESSON_INDEX'] = idx
        return idx

    def get_lesson_index():
        return app.config.get('_LESSON_INDEX') or build_lesson_index()
    @app.before_request
    def load_current_user():
        g.user = verify_session_cookie(request)

    @app.get("/")
    def index():
        try:
            featured = get_lesson_index()[:6]
        except Exception:
            featured = []
        return render_template("index.html", lessons=featured)

    @app.get("/login")
    def login():
        return render_template("login.html")

    @app.post("/sessionLogin")
    def session_login():
        data = request.get_json(silent=True) or request.form
        id_token = data.get('idToken') if data else None
        if not id_token:
            return {"error": "Missing idToken"}, 400
        # 5 days session cookie
        expires_in = timedelta(days=5)
        try:
            session_cookie = admin_auth.create_session_cookie(id_token, expires_in=expires_in)
        except Exception as e:
            return {"error": "Failed to create session cookie"}, 401
        resp = make_response({"status": "ok"})
        cookie_name = app.config["SESSION_COOKIE_NAME"]
        secure = app.config["SESSION_COOKIE_SECURE"]
        domain = app.config.get("SESSION_COOKIE_DOMAIN")
        resp.set_cookie(cookie_name, session_cookie, max_age=int(expires_in.total_seconds()), httponly=True, secure=secure, samesite='Lax', domain=domain)
        return resp

    @app.post("/sessionLogout")
    def session_logout():
        # Clear cookie
        resp = make_response({"status": "ok"})
        cookie_name = app.config["SESSION_COOKIE_NAME"]
        domain = app.config.get("SESSION_COOKIE_DOMAIN")
        resp.delete_cookie(cookie_name, domain=domain)
        return resp

    @app.get("/dashboard")
    @login_required
    def dashboard():
        # Recommend a few lessons on the dashboard
        try:
            recs = get_lesson_index()[:6]
        except Exception:
            recs = []
        return render_template("dashboard.html", user=g.user, lessons=recs)

    @app.get("/courses")
    def courses():
        # JSON-only lessons
        content_dir = _content_dir()
        lessons = []
        for root, _, files in os.walk(content_dir):
            for f in files:
                lower = f.lower()
                if lower.endswith('.json'):
                    abs_path = os.path.join(root, f)
                    rel_path = os.path.relpath(abs_path, content_dir)
                    slug = rel_path.replace(os.sep, '/').rsplit('.', 1)[0].replace('\\', '/')
                    title = f.rsplit('.', 1)[0]
                    try:
                        with open(abs_path, 'r', encoding='utf-8') as fh:
                            data = json.load(fh)
                            if isinstance(data, dict) and data.get('title'):
                                title = str(data['title']).strip() or title
                            # Optional metadata
                            summary = str(data.get('summary') or '') if isinstance(data, dict) else ''
                            tags = data.get('tags') or [] if isinstance(data, dict) else []
                            if not isinstance(tags, list):
                                tags = []
                        # Append lesson entry
                        lessons.append({
                            'slug': slug,
                            'title': title,
                            'summary': summary,
                            'tags': tags,
                        })
                    except Exception:
                        # Ignore malformed/ unreadable JSON when scanning
                        pass
        # Sort by title for stable ordering
        lessons.sort(key=lambda l: (l.get('title') or '').lower())
        return render_template("courses.html", lessons=lessons)

    @app.get('/search')
    def search():
        q = (request.args.get('q') or '').strip()
        idx = get_lesson_index()
        results = []
        if q:
            ql = q.lower()
            for item in idx:
                score = 0
                if ql in (item['title'] or '').lower():
                    score += 3
                if ql in (item['summary'] or '').lower():
                    score += 2
                if any(ql in (str(t).lower()) for t in (item['tags'] or [])):
                    score += 2
                if ql in item['text']:
                    score += 1
                if score:
                    results.append({**item, 'score': score})
            results.sort(key=lambda r: r['score'], reverse=True)
        return render_template('search.html', q=q, results=results)

    @app.get("/lesson/<path:slug>")
    def lesson(slug: str):
        content_dir = _content_dir()
        json_path = os.path.join(content_dir, f"{slug}.json")
        if not os.path.isfile(json_path):
            abort(404)
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        title = str(data.get('title') or 'Lesson')
        summary = str(data.get('summary') or '')
        tags = data.get('tags') or []
        if not isinstance(tags, list):
            tags = []
        
        # Check if this is a Pro lesson
        if 'pro' in tags:
            # User must be logged in and have Pro access
            user = verify_session_cookie(request)
            if not user or not has_pro_access(user):
                return redirect(url_for('pricing'))
        
        blocks = data.get('blocks') or []

        def _youtube_embed(url: str) -> str:
            # Basic YouTube URL to embed conversion
            yt = None
            m = re.search(r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|embed/|shorts/))([A-Za-z0-9_-]{6,})", url)
            if m:
                yt = m.group(1)
            if yt:
                # Responsive 16:9 container without relying on external CSS
                wrapper_start = '<div style="position:relative;padding-bottom:56.25%;height:0;min-height:300px;overflow:hidden;">'
                iframe = (
                    f'<iframe src="https://www.youtube.com/embed/{yt}" '
                    'title="YouTube video" frameborder="0" '
                    'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" '
                    'allowfullscreen loading="lazy" referrerpolicy="strict-origin-when-cross-origin" '
                    'style="position:absolute;top:0;left:0;width:100%;height:100%;border:0;"'
                    '></iframe>'
                )
                wrapper_end = '</div>'
                return wrapper_start + iframe + wrapper_end
            return ''

        def render_blocks(blocks) -> tuple[str, str]:
            parts = []
            toc = []
            used_ids = set()
            quiz_counter = 0
            def hesc(s: object) -> str:
                return html_mod.escape(str(s if s is not None else ''))
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                t = (b.get('type') or '').lower()
                if t == 'heading':
                    level = int(b.get('level') or 2)
                    level = max(1, min(level, 6))
                    text = str(b.get('text') or '')
                    tag = f"h{level}"
                    # Visible heading text should escape tags so they render literally
                    clean_text = hesc(text)
                    # slug id derived from raw text without angle-bracket tags
                    slug_src = re.sub(r"<[^>]+>", '', str(text))
                    base_id = re.sub(r"[^a-z0-9\- ]+", '', slug_src.lower()).strip().replace(' ', '-') or f"h{level}"
                    unique_id = base_id
                    i = 2
                    while unique_id in used_ids:
                        unique_id = f"{base_id}-{i}"
                        i += 1
                    used_ids.add(unique_id)
                    parts.append(f"<{tag} id=\"{unique_id}\">{clean_text}</{tag}>")
                    toc.append({
                        'level': level,
                        'id': unique_id,
                        'text': clean_text
                    })
                elif t == 'paragraph':
                    text = str(b.get('text') or '')
                    parts.append("<p>" + hesc(text) + "</p>")
                elif t == 'code':
                    lang = str(b.get('language') or '').lower()
                    code = str(b.get('code') or '')
                    cls = f" class=\"language-{lang}\"" if lang else ''
                    code_esc = (code
                        .replace('&', '&amp;')
                        .replace('<', '&lt;')
                        .replace('>', '&gt;'))
                    parts.append(f"<pre><code{cls}>" + code_esc + "</code></pre>")
                elif t == 'list':
                    items = b.get('items') or []
                    ordered = bool(b.get('ordered'))
                    tag = 'ol' if ordered else 'ul'
                    lis = ''.join(["<li>" + hesc(it) + "</li>" for it in items])
                    parts.append(f"<{tag}>" + lis + f"</{tag}>")
                elif t == 'table':
                    headers = b.get('headers') or []
                    rows = b.get('rows') or []
                    thead = ''
                    if headers:
                        ths = ''.join([f"<th>" + hesc(h) + "</th>" for h in headers])
                        thead = f"<thead><tr>{ths}</tr></thead>"
                    tbody_rows = []
                    for r in rows:
                        tds = ''.join([f"<td>" + hesc(c) + "</td>" for c in (r or [])])
                        tbody_rows.append(f"<tr>{tds}</tr>")
                    tbody = f"<tbody>{''.join(tbody_rows)}</tbody>"
                    parts.append(f"<div class=\"not-prose overflow-x-auto\"><table class=\"min-w-full\">{thead}{tbody}</table></div>")
                elif t == 'steps':
                    items = b.get('items') or []
                    lis = ''.join(["<li>" + hesc(it) + "</li>" for it in items])
                    parts.append("<ol>" + lis + "</ol>")
                elif t == 'image':
                    src = str(b.get('src') or '').strip()
                    alt = hesc(b.get('alt') or '')
                    if src:
                        parts.append(f'<p><img src="{src}" alt="{alt}" loading="lazy" /></p>')
                elif t == 'link':
                    url = str(b.get('url') or '').strip()
                    text = str(b.get('text') or url)
                    if url:
                        safe_text = hesc(text)
                        parts.append(f'<p><a href="{url}" target="_blank" rel="noopener noreferrer">{safe_text}</a></p>')
                elif t == 'embed':
                    url = str(b.get('url') or '').strip()
                    if 'youtube.com' in url or 'youtu.be' in url:
                        iframe = _youtube_embed(url)
                        if iframe:
                            parts.append(iframe)
                        else:
                            parts.append(f'<p><a href="{url}" target="_blank" rel="noopener noreferrer">Open video</a></p>')
                    else:
                        # Fallback to link for non-YouTube to avoid unsafe iframes
                        if url:
                            parts.append(f'<p><a href="{url}" target="_blank" rel="noopener noreferrer">Open resource</a></p>')
                elif t == 'iframe':
                    src = str(b.get('src') or '').strip()
                    title_if = hesc(b.get('title') or 'Embedded content')
                    # Optional sizing controls
                    height_val = b.get('height')
                    aspect = str(b.get('aspect') or '16:9')
                    padding_percent = '56.25%'
                    if isinstance(aspect, str) and ':' in aspect:
                        try:
                            w, h = aspect.split(':', 1)
                            w = float(w); h = float(h)
                            if w > 0 and h > 0:
                                padding_percent = f"{(h / w) * 100:.6f}%"
                        except Exception:
                            padding_percent = '56.25%'
                    if src:
                        if isinstance(height_val, (int, float)) and height_val > 0:
                            wrapper_style = f'position:relative;height:{int(height_val)}px;overflow:hidden;'
                        else:
                            wrapper_style = f'position:relative;padding-bottom:{padding_percent};height:0;min-height:300px;overflow:hidden;'
                        parts.append(
                            f'<div style="{wrapper_style}">'
                            f'<iframe src="{src}" '
                            f'title="{title_if}" frameborder="0" '
                            'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" '
                            'allowfullscreen loading="lazy" referrerpolicy="strict-origin-when-cross-origin" '
                            'style="position:absolute;top:0;left:0;width:100%;height:100%;border:0;"'
                            '></iframe>'
                            '</div>'
                        )
                elif t == 'callout':
                    kind = (b.get('kind') or 'info').lower()
                    title_co = hesc(b.get('title') or kind.title())
                    text_co = hesc(b.get('text') or '')
                    # Map kind to Tailwind colors
                    colors = {
                        'info': 'bg-sky-50 border-sky-200 text-sky-900',
                        'success': 'bg-emerald-50 border-emerald-200 text-emerald-900',
                        'warning': 'bg-amber-50 border-amber-200 text-amber-900',
                        'danger': 'bg-rose-50 border-rose-200 text-rose-900',
                        'note': 'bg-indigo-50 border-indigo-200 text-indigo-900'
                    }
                    cls = colors.get(kind, colors['info'])
                    parts.append(
                        f'<div class="not-prose my-4 p-4 border rounded {cls}">'
                        f'<div class="font-semibold mb-1">{title_co}</div>'
                        f'<div class="text-sm opacity-90">{text_co}</div>'
                        f'</div>'
                    )
                elif t == 'example':
                    lang = str(b.get('language') or '').lower()
                    code = str(b.get('code') or '')
                    runnable = bool(b.get('runnable')) and lang in ('js', 'javascript', 'html')
                    cls = f" class=\"language-{lang}\"" if lang else ''
                    code_esc = (code
                        .replace('&', '&amp;')
                        .replace('<', '&lt;')
                        .replace('>', '&gt;'))
                    btn = ''
                    if runnable:
                        btn = '<button type="button" class="vs-run-js inline-flex items-center px-3 py-1.5 rounded bg-slate-800 text-white text-sm hover:bg-slate-700">Run</button>'
                    parts.append(
                        '<div class="not-prose my-4 border rounded overflow-hidden">'
                        '<div class="px-3 py-2 border-b bg-slate-50 flex items-center justify-between">'
                        '<div class="text-xs uppercase tracking-wide text-slate-500">Example</div>'
                        f'{btn}'
                        '</div>'
                        f'<pre class="m-0"><code{cls}>' + code_esc + '</code></pre>'
                        '<div class="vs-output hidden">'
                        '<iframe class="w-full h-64" sandbox="allow-scripts allow-same-origin"></iframe>'
                        '</div>'
                        '</div>'
                    )
                elif t == 'quiz':
                    question = hesc(b.get('question') or '')
                    choices = b.get('choices') or []
                    correct = b.get('correctIndex')
                    explanation = hesc(b.get('explanation') or '')
                    if not isinstance(choices, list) or correct is None:
                        continue
                    quiz_counter += 1
                    qid = f"quiz-{quiz_counter}"
                    # Build choices
                    opts = []
                    for idx, ch in enumerate(choices):
                        label = hesc(ch)
                        rid = f"{qid}-opt-{idx}"
                        opts.append(
                            '<div class="flex items-start gap-2">'
                            f'<input id="{rid}" type="radio" name="{qid}" value="{idx}" class="mt-1">'
                            f'<label for="{rid}">{label}</label>'
                            '</div>'
                        )
                    explain_html = f'<div class="vs-quiz-explain mt-2 text-sm text-slate-600 hidden">{explanation}</div>' if explanation else ''
                    parts.append(''.join([
                        f'<div class="not-prose my-4 p-4 border rounded vs-quiz" data-answer="{int(correct)}">',
                        f'<div class="font-medium mb-2">{question}</div>',
                        ''.join(opts),
                        '<div class="mt-3 flex items-center gap-3">',
                        '<button type="button" class="vs-quiz-check px-3 py-1.5 rounded bg-indigo-600 text-white text-sm">Check</button>',
                        '<span class="vs-quiz-result text-sm"></span>',
                        '</div>',
                        explain_html,
                        '</div>'
                    ]))
                else:
                    # unknown type -> ignore
                    continue
            # Build TOC HTML
            if toc:
                toc_items = []
                for item in toc:
                    indent = (item['level'] - 2) * 12
                    indent = max(0, indent)
                    toc_items.append(
                        f'<a href="#' + item['id'] + f'" class="block pl-{indent} py-1 hover:text-indigo-600">' + item['text'] + '</a>'
                    )
                toc_html = '\n'.join(toc_items)
            else:
                toc_html = ''
            return '\n'.join(parts), toc_html

        html, toc_html = render_blocks(blocks)

        # Sanitize again as defense-in-depth
        allowed_tags = set(bleach.sanitizer.ALLOWED_TAGS).union({
            'p', 'pre', 'code', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
            'table', 'thead', 'tbody', 'tr', 'th', 'td', 'blockquote', 'ul', 'ol', 'li',
            'iframe', 'img', 'div', 'button', 'input', 'label', 'form', 'span'
        })
        allowed_attrs = {
            **bleach.sanitizer.ALLOWED_ATTRIBUTES,
            'a': ['href', 'title', 'rel', 'target', 'class'],
            'code': ['class'],
            'p': ['class'],
            'pre': ['class'],
            'h1': ['id', 'class'], 'h2': ['id', 'class'], 'h3': ['id', 'class'], 'h4': ['id', 'class'], 'h5': ['id', 'class'], 'h6': ['id', 'class'],
            'ul': ['class'], 'ol': ['class'], 'li': ['class'],
            'table': ['class'], 'thead': ['class'], 'tbody': ['class'], 'tr': ['class'], 'th': ['align', 'class'], 'td': ['align', 'class'],
            'th': ['align'],
            'td': ['align'],
            'iframe': ['src', 'width', 'height', 'allow', 'allowfullscreen', 'frameborder', 'loading', 'referrerpolicy', 'title', 'class', 'style'],
            'img': ['src', 'alt', 'title', 'width', 'height', 'loading', 'class'],
            'div': ['class', 'data-answer', 'style'],
            'button': ['class', 'type'],
            'input': ['class', 'type', 'name', 'value', 'id', 'checked'],
            'label': ['for', 'class'],
            'form': ['class'],
            'span': ['class']
        }
        css = CSSSanitizer(
            allowed_css_properties=[
                'position', 'padding', 'padding-bottom', 'height', 'overflow',
                'top', 'left', 'width', 'border', 'max-width', 'min-height'
            ]
        )
        html = bleach.clean(
            html,
            tags=list(allowed_tags),
            attributes=allowed_attrs,
            protocols=['http', 'https', 'mailto'],
            strip=True,
            css_sanitizer=css,
        )
        # refresh index lazily when content is requested (simple approach)
        app.config['_LESSON_INDEX'] = None
        return render_template('lesson.html', title=title, slug=slug, content_html=html, toc_html=toc_html, summary=summary, tags=tags)

    # -------------------- Progress tracking --------------------
    def _data_dir() -> str:
        d = os.path.join(app.root_path, 'data')
        os.makedirs(d, exist_ok=True)
        return d

    def _progress_file(uid: str) -> str:
        return os.path.join(_data_dir(), f"progress_{uid}.json")

    def _load_progress(uid: str) -> dict:
        import json
        path = _progress_file(uid)
        if os.path.isfile(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return {"completed": []}
        return {"completed": []}

    def _save_progress(uid: str, data: dict) -> None:
        import json
        path = _progress_file(uid)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _all_lesson_slugs() -> list:
        content_dir = _content_dir()
        slugs = set()
        for root, _, files in os.walk(content_dir):
            for f in files:
                lower = f.lower()
                if lower.endswith('.json'):
                    abs_path = os.path.join(root, f)
                    rel_path = os.path.relpath(abs_path, content_dir)
                    slug = rel_path.replace(os.sep, '/').rsplit('.', 1)[0].replace('\\', '/')
                    slugs.add(slug)
        return sorted(slugs)

    @app.get('/api/progress')
    @login_required
    def get_progress():
        uid = g.user.get('uid')
        prog = _load_progress(uid)
        total = len(_all_lesson_slugs())
        completed = list(set(prog.get('completed', [])))
        percent = int(round((len(completed) / total) * 100)) if total else 0
        return {"completed": completed, "total": total, "percent": percent}

    @app.post('/api/progress')
    @login_required
    def update_progress():
        data = request.get_json(silent=True) or {}
        slug = (data.get('slug') or '').strip()
        done = bool(data.get('completed', True))
        if not slug:
            return {"error": "Missing slug"}, 400
        uid = g.user.get('uid')
        prog = _load_progress(uid)
        comp = set(prog.get('completed', []))
        if done:
            comp.add(slug)
        else:
            comp.discard(slug)
        prog['completed'] = sorted(comp)
        _save_progress(uid, prog)
        total = len(_all_lesson_slugs())
        percent = int(round((len(comp) / total) * 100)) if total else 0
        return {"completed": list(comp), "total": total, "percent": percent}

    # -------------------- Pro Routes --------------------
    @app.get('/admin/register-pro/<uid>')
    def admin_register_pro(uid: str):
        """Admin endpoint to manually register a Pro user"""
        register_pro_user(uid, f"admin_registered_{uid}")
        return {"status": "success", "uid": uid, "message": "User registered as Pro"}

    @app.get('/pro')
    @login_required
    def pro_dashboard():
        # Check if user has Pro access (paid or allowed email)
        if not has_pro_access(g.user):
            return redirect(url_for('pricing'))
        
        # Get Pro lessons (lessons with 'pro' tag)
        all_lessons = get_lesson_index()
        pro_lessons = [lesson for lesson in all_lessons if 'pro' in lesson.get('tags', [])]
        
        return render_template('pro/dashboard.html', lessons=pro_lessons)

    @app.get('/pricing')
    def pricing():
        return render_template('pricing.html', stripe_publishable_key=stripe_publishable_key)

    @app.post('/create-checkout-session')
    @login_required
    def create_checkout_session():
        try:
            checkout_session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                mode='payment',
                line_items=[{
                    'price_data': {
                        'currency': 'gbp',
                        'product_data': {
                            'name': 'INFRA+- Pro Access',
                            'description': 'Lifetime access to all Pro content',
                        },
                        'unit_amount': 999,  # £9.99
                    },
                    'quantity': 1,
                }],
                success_url=url_for('payment_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
                cancel_url=url_for('pricing', _external=True),
                customer_email=g.user.get('email'),
                metadata={
                    'user_id': g.user.get('uid'),
                    'user_email': g.user.get('email'),
                }
            )
            return {'id': checkout_session.id}
        except Exception as e:
            return {'error': str(e)}, 400

    @app.get('/payment/success')
    @login_required
    def payment_success():
        session_id = request.args.get('session_id')
        if not session_id:
            return redirect(url_for('pricing'))
        
        try:
            # Verify the session
            session = stripe.checkout.Session.retrieve(session_id)
            if session.payment_status == 'paid':
                # Register user as Pro
                uid = g.user.get('uid')
                email = g.user.get('email')
                register_pro_user(uid, email)
                
                # Also set Firebase claims as backup
                try:
                    admin_auth.set_custom_user_claims(uid, {'pro': True})
                except:
                    pass  # Ignore Firebase errors
                
                return render_template('payment_success.html', session=session)
            else:
                return redirect(url_for('pricing'))
        except Exception as e:
            print(f"Payment success error: {e}")
            return redirect(url_for('pricing'))

    @app.post('/stripe/webhook')
    def stripe_webhook():
        payload = request.data
        sig_header = request.headers.get('stripe-signature')

        try:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        except ValueError:
            return 'Invalid payload', 400
        except stripe.error.SignatureVerificationError:
            return 'Invalid signature', 400

        # Handle the event
        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            user_id = session.metadata.get('user_id')
            if user_id:
                # Grant Pro access
                admin_auth.set_custom_user_claims(user_id, {'pro': True})

        return 'Success', 200

    # Health check
    @app.get('/healthz')
    def healthz():
        return {"status": "ok"}, 200

    # Error handlers
    @app.errorhandler(404)
    def not_found(e):
        return render_template('404.html'), 404

    @app.errorhandler(500)
    def server_error(e):
        return render_template('500.html'), 500

    # Security headers
    @app.after_request
    def set_security_headers(resp):
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('X-Frame-Options', 'DENY')
        resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        # Only set HSTS if cookies are secure (assume HTTPS in that case)
        if app.config.get("SESSION_COOKIE_SECURE"):
            resp.headers.setdefault('Strict-Transport-Security', 'max-age=63072000; includeSubDomains; preload')
        # A relaxed CSP suitable for this app (adjust as needed)
        # CSP removed for development - re-add before production with proper configuration
        # resp.headers.setdefault(
        #     'Content-Security-Policy',
        #     "default-src 'self'; "
        #     "script-src 'self' 'unsafe-inline' https://www.gstatic.com https://apis.google.com https://cdn.tailwindcss.com https://js.stripe.com https://kit.webawesome.com; "
        #     "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        #     "font-src 'self' https://fonts.gstatic.com; "
        #     "img-src 'self' data: https:; "
        #     "connect-src 'self' https://www.googleapis.com https://identitytoolkit.googleapis.com https://securetoken.googleapis.com https://oauth2.googleapis.com https://apis.google.com https://api.stripe.com; "
        #     "frame-src 'self' https://accounts.google.com https://*.google.com https://*.gstatic.com https://*.firebaseapp.com https://www.youtube.com https://www.youtube-nocookie.com https://checkout.stripe.com https://js.stripe.com;"
        # )
        return resp

    @app.get('/config.js')
    def config_js():
        # Provide Firebase Web SDK config to client
        cfg = {
            'apiKey': os.getenv('FIREBASE_WEB_API_KEY', ''),
            'authDomain': os.getenv('FIREBASE_WEB_AUTH_DOMAIN', ''),
            'projectId': os.getenv('FIREBASE_PROJECT_ID', ''),
            'appId': os.getenv('FIREBASE_WEB_APP_ID', ''),
            'messagingSenderId': os.getenv('FIREBASE_WEB_MESSAGING_SENDER_ID', ''),
        }
        js = (
            'window.FIREBASE_CONFIG = ' + __import__('json').dumps(cfg) + ';\n'
        )
        resp = make_response(js)
        resp.headers['Content-Type'] = 'application/javascript'
        return resp

    @app.get('/support')
    def support():
        return render_template('support.html')

    return app


app = create_app()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)

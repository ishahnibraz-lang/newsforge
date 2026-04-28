from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, date, timedelta
from urllib.parse import urljoin, urlparse
import anthropic, requests, os, re, json, base64, io
from bs4 import BeautifulSoup
import feedparser

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get('SECRET_KEY', 'newsforge-saas-secret-2024'),
    SQLALCHEMY_DATABASE_URI=os.environ.get('DATABASE_URL', 'sqlite:///newsforge.db').replace('postgres://', 'postgresql://'),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    MAX_CONTENT_LENGTH=32 * 1024 * 1024,
)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

app.jinja_env.filters['from_json'] = json.loads


# ── MODELS ───────────────────────────────────────────────────────────

class Plan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    daily_limit = db.Column(db.Integer, default=20)
    price = db.Column(db.Float, default=0.0)
    features = db.Column(db.Text, default='[]')   # JSON list of feature strings
    social_accounts = db.Column(db.Integer, default=1)
    has_wordpress = db.Column(db.Boolean, default=False)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    role = db.Column(db.String(20), default='staff')
    plan_id = db.Column(db.Integer, db.ForeignKey('plan.id'), default=1)
    daily_used = db.Column(db.Integer, default=0)
    last_reset = db.Column(db.Date, default=date.today)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    plan = db.relationship('Plan', backref='users')

    def set_password(self, pw): self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)

    @property
    def daily_limit(self): return self.plan.daily_limit if self.plan else 20

    def _maybe_reset(self):
        if self.last_reset != date.today():
            self.daily_used = 0; self.last_reset = date.today(); db.session.commit()

    @property
    def credits_remaining(self):
        self._maybe_reset()
        return max(0, self.daily_limit - self.daily_used)

    def use_credit(self, desc='AI Rewrite'):
        self._maybe_reset()
        if self.credits_remaining <= 0: return False
        self.daily_used += 1
        db.session.add(CreditTransaction(user_id=self.id, amount=-1, description=desc))
        db.session.commit()
        return True


class News(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(500), nullable=False)
    content = db.Column(db.Text, default='')
    image_url = db.Column(db.String(1000), default='')
    source_url = db.Column(db.String(1000), default='')
    status = db.Column(db.String(20), default='draft')
    card_data = db.Column(db.Text, default='{}')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    published_at = db.Column(db.DateTime)
    author = db.relationship('User', backref='news_items')


class NewsSource(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SocialConnection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    platform = db.Column(db.String(30), nullable=False)   # facebook | instagram | twitter | wordpress
    label = db.Column(db.String(100), default='')
    credentials = db.Column(db.Text, default='{}')        # JSON — tokens, IDs, URLs
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship('User', backref='connections')


class PublishLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    news_id = db.Column(db.Integer, db.ForeignKey('news.id'), nullable=False)
    platform = db.Column(db.String(30))
    status = db.Column(db.String(20), default='pending')  # success | failed
    response = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    news = db.relationship('News', backref='publish_logs')


class CreditTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    description = db.Column(db.String(200), default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship('User', backref='transactions')


@login_manager.user_loader
def load_user(uid): return User.query.get(int(uid))


def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*a, **kw):
        if current_user.role != 'admin':
            flash('Admin access required.', 'error')
            return redirect(url_for('feed'))
        return f(*a, **kw)
    return decorated


# ── PUBLIC LANDING ────────────────────────────────────────────────────

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('feed'))
    plans = Plan.query.order_by(Plan.price).all()
    return render_template('landing.html', plans=plans)


# ── AUTH ─────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('feed'))
    if request.method == 'POST':
        user = User.query.filter_by(email=request.form['email']).first()
        if user and user.check_password(request.form['password']):
            if not user.is_active:
                flash('Account deactivated. Contact admin.', 'error')
                return redirect(url_for('login'))
            login_user(user, remember=True)
            return redirect(url_for('feed'))
        flash('Invalid email or password.', 'error')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('feed'))
    if request.method == 'POST':
        if User.query.filter_by(email=request.form['email']).first():
            flash('Email already registered.', 'error')
            return redirect(url_for('login'))
        plan = Plan.query.filter_by(name='Free').first() or Plan.query.first()
        user = User(name=request.form['name'], email=request.form['email'], plan_id=plan.id if plan else 1)
        user.set_password(request.form['password'])
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(url_for('feed'))
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


# ── DASHBOARD ─────────────────────────────────────────────────────────

@app.route('/feed')
@login_required
def feed():
    sources = NewsSource.query.filter_by(user_id=current_user.id, is_active=True).all()
    return render_template('feed.html', sources=sources)


@app.route('/published')
@login_required
def published():
    news = News.query.filter_by(user_id=current_user.id, status='published').order_by(News.published_at.desc()).all()
    connections = SocialConnection.query.filter_by(user_id=current_user.id, is_active=True).all()
    connections_data = [{'id': c.id, 'platform': c.platform, 'label': c.label or c.platform} for c in connections]
    return render_template('published.html', news=news, connections=connections, connections_data=connections_data)


@app.route('/create', methods=['GET', 'POST'])
@login_required
def create():
    if request.method == 'POST':
        action = request.form.get('action', 'draft')
        news = News(
            user_id=current_user.id,
            title=request.form['title'],
            content=request.form.get('content', ''),
            image_url=request.form.get('image_url', ''),
            source_url=request.form.get('source_url', ''),
            status='published' if action == 'publish' else 'draft',
        )
        if news.status == 'published':
            news.published_at = datetime.utcnow()
        db.session.add(news)
        db.session.commit()
        flash('Published!' if news.status == 'published' else 'Saved as draft.', 'success')
        return redirect(url_for('published' if news.status == 'published' else 'drafts'))
    return render_template('create.html', news=None)


@app.route('/news/<int:nid>/edit', methods=['GET', 'POST'])
@login_required
def edit_news(nid):
    news = News.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    if request.method == 'POST':
        news.title = request.form['title']
        news.content = request.form.get('content', '')
        news.image_url = request.form.get('image_url', '')
        news.source_url = request.form.get('source_url', '')
        db.session.commit()
        flash('Updated!', 'success')
        return redirect(url_for('published'))
    return render_template('create.html', news=news, editing=True)


@app.route('/drafts')
@login_required
def drafts():
    news = News.query.filter_by(user_id=current_user.id, status='draft').order_by(News.created_at.desc()).all()
    return render_template('drafts.html', news=news)


@app.route('/observe')
@login_required
def observe():
    sources = NewsSource.query.filter_by(user_id=current_user.id).order_by(NewsSource.created_at.desc()).all()
    return render_template('observe.html', sources=sources)


@app.route('/news/<int:nid>/studio')
@login_required
def studio(nid):
    news = News.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    return render_template('studio.html', news=news)


@app.route('/credits')
@login_required
def credits_page():
    txns = CreditTransaction.query.filter_by(user_id=current_user.id).order_by(CreditTransaction.created_at.desc()).limit(50).all()
    return render_template('credits.html', transactions=txns)


@app.route('/connections')
@login_required
def connections():
    conns = SocialConnection.query.filter_by(user_id=current_user.id).all()
    logs = PublishLog.query.filter_by(user_id=current_user.id).order_by(PublishLog.created_at.desc()).limit(20).all()
    return render_template('connections.html', connections=conns, logs=logs)


# ── ADMIN ─────────────────────────────────────────────────────────────

@app.route('/admin')
@admin_required
def admin_index():
    users = User.query.order_by(User.created_at.desc()).all()
    plans = Plan.query.all()
    today_news = News.query.filter(News.created_at >= datetime.utcnow().replace(hour=0, minute=0)).count()
    stats = {
        'total_users': User.query.filter_by(role='staff').count(),
        'total_news': News.query.count(),
        'published': News.query.filter_by(status='published').count(),
        'active_users': User.query.filter_by(is_active=True, role='staff').count(),
        'today_news': today_news,
        'total_publishes': PublishLog.query.filter_by(status='success').count(),
    }
    recent_logs = PublishLog.query.order_by(PublishLog.created_at.desc()).limit(10).all()
    return render_template('admin/index.html', users=users, plans=plans, stats=stats, logs=recent_logs)


@app.route('/admin/users/add', methods=['POST'])
@admin_required
def admin_add_user():
    data = request.json
    if not data.get('email') or not data.get('password'):
        return jsonify({'error': 'Email and password required'}), 400
    if User.query.filter_by(email=data['email']).first():
        return jsonify({'error': 'Email already registered'}), 400
    user = User(name=data.get('name', data['email']), email=data['email'],
                role=data.get('role', 'staff'), plan_id=int(data.get('plan_id', 1)))
    user.set_password(data['password'])
    db.session.add(user)
    db.session.commit()
    return jsonify({'ok': True, 'id': user.id})


@app.route('/admin/users/<int:uid>', methods=['POST'])
@admin_required
def admin_update_user(uid):
    user = User.query.get_or_404(uid)
    data = request.json
    if 'plan_id' in data: user.plan_id = int(data['plan_id'])
    if 'role' in data: user.role = data['role']
    if 'is_active' in data: user.is_active = bool(data['is_active'])
    if 'add_credits' in data:
        n = int(data['add_credits'])
        user.daily_used = max(0, user.daily_used - n)
        db.session.add(CreditTransaction(user_id=uid, amount=n, description='Admin top-up'))
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/admin/users/<int:uid>/delete', methods=['DELETE'])
@admin_required
def admin_delete_user(uid):
    if uid == current_user.id:
        return jsonify({'error': 'Cannot delete yourself'}), 400
    db.session.delete(User.query.get_or_404(uid))
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/admin/plans', methods=['POST'])
@admin_required
def admin_add_plan():
    data = request.json
    plan = Plan(name=data['name'], daily_limit=int(data['daily_limit']), price=float(data.get('price', 0)),
                social_accounts=int(data.get('social_accounts', 1)), has_wordpress=bool(data.get('has_wordpress', False)))
    db.session.add(plan)
    db.session.commit()
    return jsonify({'ok': True, 'id': plan.id})


@app.route('/admin/plans/<int:pid>', methods=['PUT', 'DELETE'])
@admin_required
def admin_manage_plan(pid):
    plan = Plan.query.get_or_404(pid)
    if request.method == 'DELETE':
        db.session.delete(plan); db.session.commit()
        return jsonify({'ok': True})
    data = request.json
    for k in ('name', 'daily_limit', 'price', 'social_accounts', 'has_wordpress'):
        if k in data:
            setattr(plan, k, data[k])
    db.session.commit()
    return jsonify({'ok': True})


# ── NEWS API ─────────────────────────────────────────────────────────

@app.route('/api/rewrite', methods=['POST'])
@login_required
def api_rewrite():
    if not current_user.use_credit('AI Rewrite'):
        return jsonify({'error': 'Daily credit limit reached. Upgrade your plan.'}), 429
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        current_user.daily_used = max(0, current_user.daily_used - 1); db.session.commit()
        return jsonify({'error': 'ANTHROPIC_API_KEY not set in Railway environment.'}), 500
    data = request.json
    try:
        client = anthropic.Anthropic(api_key=api_key)
        lang = data.get('language', 'Bengali')
        msg = client.messages.create(
            model='claude-sonnet-4-6', max_tokens=1500,
            messages=[{'role': 'user', 'content': f"""Rewrite this news professionally in {lang}. Engaging, accurate, social-media ready.

Title: {data.get('title', '')}
Content: {data.get('content', '')}

Return raw JSON only (no markdown):
{{"rewritten_title": "...", "rewritten_content": "...", "summary": "2-3 sentence summary", "hashtags": ["#tag1","#tag2","#tag3"]}}"""}]
        )
        text = re.sub(r'^```(?:json)?\s*\n?', '', msg.content[0].text.strip())
        text = re.sub(r'\n?```\s*$', '', text)
        return jsonify(json.loads(text.strip()))
    except Exception as e:
        current_user.daily_used = max(0, current_user.daily_used - 1); db.session.commit()
        return jsonify({'error': str(e)}), 500


@app.route('/api/news/<int:nid>/publish', methods=['POST'])
@login_required
def api_publish(nid):
    news = News.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    news.status = 'published'; news.published_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/news/<int:nid>/delete', methods=['DELETE'])
@login_required
def api_delete_news(nid):
    news = News.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    db.session.delete(news); db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/news/<int:nid>/card', methods=['POST'])
@login_required
def api_save_card(nid):
    news = News.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    news.card_data = request.data.decode(); db.session.commit()
    return jsonify({'ok': True})


# ── SOURCES & SCRAPING ────────────────────────────────────────────────

@app.route('/api/sources', methods=['POST'])
@login_required
def api_add_source():
    data = request.json
    url = data.get('url', '').strip()
    if not url.startswith('http'): url = 'https://' + url
    name = data.get('name', '').strip() or urlparse(url).netloc.replace('www.', '')
    src = NewsSource(user_id=current_user.id, name=name, url=url)
    db.session.add(src); db.session.commit()
    return jsonify({'ok': True, 'id': src.id, 'name': src.name, 'url': src.url})


@app.route('/api/sources/<int:sid>', methods=['DELETE'])
@login_required
def api_delete_source(sid):
    src = NewsSource.query.filter_by(id=sid, user_id=current_user.id).first_or_404()
    db.session.delete(src); db.session.commit()
    return jsonify({'ok': True})


def _extract_image(entry):
    """Extract best image from a feed entry."""
    # media:thumbnail
    if hasattr(entry, 'media_thumbnail') and entry.media_thumbnail:
        return entry.media_thumbnail[0].get('url', '')
    # media:content
    if hasattr(entry, 'media_content') and entry.media_content:
        for mc in entry.media_content:
            if mc.get('url', '') and 'image' in mc.get('type', 'image'):
                return mc['url']
    # enclosures
    for enc in getattr(entry, 'enclosures', []):
        if 'image' in enc.get('type', ''):
            return enc.get('href', enc.get('url', ''))
    # og:image in summary HTML
    summary_html = entry.get('summary', '') or ''
    soup = BeautifulSoup(summary_html, 'html.parser')
    img = soup.find('img')
    if img and img.get('src'):
        return img['src']
    return ''


@app.route('/api/sources/<int:sid>/scrape', methods=['POST'])
@login_required
def api_scrape(sid):
    src = NewsSource.query.filter_by(id=sid, user_id=current_user.id).first_or_404()
    cutoff = datetime.utcnow() - timedelta(hours=48)
    items = []

    # 1 — Try RSS/Atom feed
    rss_candidates = [
        src.url,
        src.url.rstrip('/') + '/feed',
        src.url.rstrip('/') + '/rss',
        src.url.rstrip('/') + '/feed.xml',
        src.url.rstrip('/') + '/rss.xml',
        src.url.rstrip('/') + '/atom.xml',
    ]
    for rss_url in rss_candidates:
        try:
            feed = feedparser.parse(rss_url)
            if not feed.entries: continue
            for entry in feed.entries[:30]:
                # Date filter
                pub = None
                for attr in ('published_parsed', 'updated_parsed', 'created_parsed'):
                    if getattr(entry, attr, None):
                        try:
                            pub = datetime(*getattr(entry, attr)[:6])
                        except Exception:
                            pass
                        break
                if pub and pub < cutoff:
                    continue   # skip old articles
                img = _extract_image(entry)
                items.append({
                    'title': entry.get('title', '').strip(),
                    'url': entry.get('link', ''),
                    'image_url': img,
                    'source': src.name,
                    'published': pub.strftime('%d %b %Y, %I:%M %p') if pub else 'Recent',
                    'summary': BeautifulSoup(entry.get('summary', ''), 'html.parser').get_text()[:200],
                })
                if len(items) >= 20: break
            if items:
                return jsonify({'items': items, 'method': 'rss'})
        except Exception:
            continue

    # 2 — HTML fallback with better image extraction
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0'}
        r = requests.get(src.url, headers=headers, timeout=12)
        soup = BeautifulSoup(r.text, 'html.parser')
        seen = set()
        for tag in soup.find_all(['article', 'div', 'li'], limit=60):
            a = tag.find('a', href=True)
            if not a: continue
            title = a.get_text().strip()
            if len(title) < 25 or title in seen: continue
            seen.add(title)
            href = a['href']
            if not href.startswith('http'):
                href = urljoin(src.url, href)
            # Image: try within the container
            img_tag = tag.find('img')
            img_url = ''
            if img_tag:
                img_url = img_tag.get('src') or img_tag.get('data-src') or img_tag.get('data-lazy-src') or ''
                if img_url and not img_url.startswith('http'):
                    img_url = urljoin(src.url, img_url)
            items.append({'title': title, 'url': href, 'image_url': img_url, 'source': src.name, 'published': 'Recent', 'summary': ''})
            if len(items) >= 20: break
        return jsonify({'items': items, 'method': 'html'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── SOCIAL CONNECTIONS ────────────────────────────────────────────────

@app.route('/api/connections', methods=['POST'])
@login_required
def api_add_connection():
    data = request.json
    platform = data.get('platform', '')
    label = data.get('label', platform)
    creds = data.get('credentials', {})
    # Enforce plan social account limit
    existing = SocialConnection.query.filter_by(user_id=current_user.id, is_active=True).count()
    if current_user.plan and existing >= current_user.plan.social_accounts:
        return jsonify({'error': f'Your plan allows {current_user.plan.social_accounts} social connections. Upgrade to add more.'}), 403
    conn = SocialConnection(user_id=current_user.id, platform=platform, label=label, credentials=json.dumps(creds))
    db.session.add(conn); db.session.commit()
    return jsonify({'ok': True, 'id': conn.id})


@app.route('/api/connections/<int:cid>', methods=['DELETE'])
@login_required
def api_delete_connection(cid):
    conn = SocialConnection.query.filter_by(id=cid, user_id=current_user.id).first_or_404()
    db.session.delete(conn); db.session.commit()
    return jsonify({'ok': True})


# ── SOCIAL PUBLISHING ─────────────────────────────────────────────────

def _log_publish(news_id, platform, status, response):
    db.session.add(PublishLog(user_id=current_user.id, news_id=news_id, platform=platform,
                              status=status, response=str(response)[:1000]))
    db.session.commit()


@app.route('/api/publish/facebook/<int:nid>', methods=['POST'])
@login_required
def publish_facebook(nid):
    news = News.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    data = request.json
    conn = SocialConnection.query.filter_by(id=data.get('conn_id'), user_id=current_user.id).first_or_404()
    creds = json.loads(conn.credentials)
    page_id = creds.get('page_id', '')
    token = creds.get('access_token', '')
    message = data.get('message', news.title)
    card_image = data.get('card_image_url', news.image_url)
    news_link = data.get('news_link', news.source_url)

    try:
        if card_image:
            # Post photo with caption
            url = f'https://graph.facebook.com/v19.0/{page_id}/photos'
            payload = {'url': card_image, 'caption': message, 'access_token': token}
            if news_link:
                payload['caption'] += f'\n\n🔗 {news_link}'
            r = requests.post(url, data=payload, timeout=15)
        else:
            url = f'https://graph.facebook.com/v19.0/{page_id}/feed'
            payload = {'message': message, 'access_token': token}
            if news_link: payload['link'] = news_link
            r = requests.post(url, data=payload, timeout=15)
        result = r.json()
        if 'error' in result:
            _log_publish(nid, 'facebook', 'failed', result)
            return jsonify({'error': result['error'].get('message', 'Facebook error')}), 400
        _log_publish(nid, 'facebook', 'success', result)
        return jsonify({'ok': True, 'post_id': result.get('id', '')})
    except Exception as e:
        _log_publish(nid, 'facebook', 'failed', str(e))
        return jsonify({'error': str(e)}), 500


@app.route('/api/publish/instagram/<int:nid>', methods=['POST'])
@login_required
def publish_instagram(nid):
    news = News.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    data = request.json
    conn = SocialConnection.query.filter_by(id=data.get('conn_id'), user_id=current_user.id).first_or_404()
    creds = json.loads(conn.credentials)
    ig_id = creds.get('ig_user_id', '')
    token = creds.get('access_token', '')
    caption = data.get('message', news.title)
    image_url = data.get('card_image_url', news.image_url)
    news_link = data.get('news_link', news.source_url)

    if not image_url:
        return jsonify({'error': 'Instagram requires an image. Add an image to the article first.'}), 400
    if news_link:
        caption += f'\n\n🔗 Source: {news_link}'

    try:
        # Step 1: Create media container
        r1 = requests.post(f'https://graph.facebook.com/v19.0/{ig_id}/media',
                           data={'image_url': image_url, 'caption': caption, 'access_token': token}, timeout=15)
        d1 = r1.json()
        if 'error' in d1:
            _log_publish(nid, 'instagram', 'failed', d1)
            return jsonify({'error': d1['error'].get('message', 'Instagram error')}), 400
        container_id = d1.get('id')

        # Step 2: Publish
        r2 = requests.post(f'https://graph.facebook.com/v19.0/{ig_id}/media_publish',
                           data={'creation_id': container_id, 'access_token': token}, timeout=15)
        d2 = r2.json()
        if 'error' in d2:
            _log_publish(nid, 'instagram', 'failed', d2)
            return jsonify({'error': d2['error'].get('message', 'Instagram publish error')}), 400
        _log_publish(nid, 'instagram', 'success', d2)
        return jsonify({'ok': True, 'media_id': d2.get('id', '')})
    except Exception as e:
        _log_publish(nid, 'instagram', 'failed', str(e))
        return jsonify({'error': str(e)}), 500


@app.route('/api/publish/twitter/<int:nid>', methods=['POST'])
@login_required
def publish_twitter(nid):
    news = News.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    data = request.json
    conn = SocialConnection.query.filter_by(id=data.get('conn_id'), user_id=current_user.id).first_or_404()
    creds = json.loads(conn.credentials)
    text = data.get('message', news.title)
    news_link = data.get('news_link', news.source_url)
    if news_link: text += f'\n\n{news_link}'
    if len(text) > 280: text = text[:277] + '...'

    try:
        import tweepy
        client = tweepy.Client(
            consumer_key=creds.get('api_key'), consumer_secret=creds.get('api_secret'),
            access_token=creds.get('access_token'), access_token_secret=creds.get('access_token_secret')
        )
        tweet = client.create_tweet(text=text)
        _log_publish(nid, 'twitter', 'success', tweet.data)
        return jsonify({'ok': True, 'tweet_id': str(tweet.data['id'])})
    except Exception as e:
        _log_publish(nid, 'twitter', 'failed', str(e))
        return jsonify({'error': str(e)}), 500


@app.route('/api/publish/wordpress/<int:nid>', methods=['POST'])
@login_required
def publish_wordpress(nid):
    if not (current_user.plan and current_user.plan.has_wordpress):
        return jsonify({'error': 'WordPress integration requires Pro or Agency plan.'}), 403
    news = News.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    data = request.json
    conn = SocialConnection.query.filter_by(id=data.get('conn_id'), user_id=current_user.id).first_or_404()
    creds = json.loads(conn.credentials)
    site_url = creds.get('site_url', '').rstrip('/')
    username = creds.get('username', '')
    app_password = creds.get('app_password', '')

    token = base64.b64encode(f'{username}:{app_password}'.encode()).decode()
    headers = {'Authorization': f'Basic {token}', 'Content-Type': 'application/json'}

    content = news.content
    if news.source_url:
        content += f'\n\n<p><strong>Source:</strong> <a href="{news.source_url}">{news.source_url}</a></p>'
    if news.image_url:
        content = f'<img src="{news.image_url}" style="max-width:100%;height:auto;"/>\n\n' + content

    try:
        post_data = {'title': news.title, 'content': content, 'status': 'publish'}
        r = requests.post(f'{site_url}/wp-json/wp/v2/posts', json=post_data, headers=headers, timeout=15)
        result = r.json()
        if r.status_code not in (200, 201):
            _log_publish(nid, 'wordpress', 'failed', result)
            return jsonify({'error': result.get('message', f'WordPress error {r.status_code}')}), 400
        wp_url = result.get('link', site_url)
        _log_publish(nid, 'wordpress', 'success', {'url': wp_url, 'id': result.get('id')})
        return jsonify({'ok': True, 'url': wp_url, 'post_id': result.get('id')})
    except Exception as e:
        _log_publish(nid, 'wordpress', 'failed', str(e))
        return jsonify({'error': str(e)}), 500


# ── INIT ─────────────────────────────────────────────────────────────

def init_db():
    db.create_all()
    if Plan.query.count() == 0:
        db.session.add_all([
            Plan(name='Free', daily_limit=5, price=0, social_accounts=0, has_wordpress=False,
                 features='["5 AI rewrites/day","Feed monitoring","Design Studio","Download card"]'),
            Plan(name='Starter', daily_limit=30, price=14.99, social_accounts=1, has_wordpress=False,
                 features='["30 AI rewrites/day","1 Social account (FB/IG/X)","Auto-publish to social","Feed monitoring","Design Studio"]'),
            Plan(name='Pro', daily_limit=100, price=39.99, social_accounts=3, has_wordpress=True,
                 features='["100 AI rewrites/day","3 Social accounts","WordPress publishing","Auto news link in comments","Priority support"]'),
            Plan(name='Agency', daily_limit=500, price=99.99, social_accounts=10, has_wordpress=True,
                 features='["500 AI rewrites/day","10 Social accounts","WordPress publishing","Bulk publishing","White-label ready","Dedicated support"]'),
        ])
        db.session.commit()
    if User.query.count() == 0:
        admin = User(name='Admin', email='admin@newsforge.com', role='admin', plan_id=4)
        admin.set_password('admin123')
        db.session.add(admin)
        db.session.commit()
        print('\n  Default admin → admin@newsforge.com / admin123\n')


with app.app_context():
    init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_ENV') == 'development')

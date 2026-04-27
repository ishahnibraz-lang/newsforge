from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, date
from urllib.parse import urljoin
import anthropic, requests, os, re, json
from bs4 import BeautifulSoup

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get('SECRET_KEY', 'newsforge-saas-secret-2024'),
    SQLALCHEMY_DATABASE_URI=os.environ.get('DATABASE_URL', 'sqlite:///newsforge.db').replace('postgres://', 'postgresql://'),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'


# ── MODELS ───────────────────────────────────────────────────────────

class Plan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    daily_limit = db.Column(db.Integer, default=20)
    price = db.Column(db.Float, default=0.0)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    role = db.Column(db.String(20), default='staff')   # admin | staff
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
            self.daily_used = 0
            self.last_reset = date.today()
            db.session.commit()

    @property
    def credits_remaining(self):
        self._maybe_reset()
        return max(0, self.daily_limit - self.daily_used)

    def use_credit(self, desc='AI Rewrite'):
        self._maybe_reset()
        if self.credits_remaining <= 0:
            return False
        self.daily_used += 1
        db.session.add(CreditTransaction(user_id=self.id, amount=-1, description=desc))
        db.session.commit()
        return True


class News(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(500), nullable=False)
    content = db.Column(db.Text, default='')
    image_url = db.Column(db.String(500), default='')
    status = db.Column(db.String(20), default='draft')   # draft | published
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
            return redirect(url_for('register'))
        plan = Plan.query.filter_by(name='Free').first() or Plan.query.first()
        user = User(name=request.form['name'], email=request.form['email'], plan_id=plan.id if plan else 1)
        user.set_password(request.form['password'])
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(url_for('feed'))
    return render_template('register.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ── DASHBOARD ─────────────────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    return redirect(url_for('feed'))


@app.route('/feed')
@login_required
def feed():
    sources = NewsSource.query.filter_by(user_id=current_user.id, is_active=True).all()
    return render_template('feed.html', sources=sources, feed_items=[])


@app.route('/published')
@login_required
def published():
    news = News.query.filter_by(user_id=current_user.id, status='published').order_by(News.published_at.desc()).all()
    return render_template('published.html', news=news)


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


# ── ADMIN ─────────────────────────────────────────────────────────────

@app.route('/admin')
@admin_required
def admin_index():
    users = User.query.order_by(User.created_at.desc()).all()
    plans = Plan.query.all()
    stats = {
        'total_users': User.query.filter_by(role='staff').count(),
        'total_news': News.query.count(),
        'published': News.query.filter_by(status='published').count(),
        'active_users': User.query.filter_by(is_active=True).count(),
    }
    return render_template('admin/index.html', users=users, plans=plans, stats=stats)


@app.route('/admin/users/<int:uid>', methods=['POST'])
@admin_required
def admin_update_user(uid):
    user = User.query.get_or_404(uid)
    data = request.json
    if 'plan_id' in data:
        user.plan_id = int(data['plan_id'])
    if 'role' in data:
        user.role = data['role']
    if 'is_active' in data:
        user.is_active = bool(data['is_active'])
    if 'add_credits' in data:
        n = int(data['add_credits'])
        user.daily_used = max(0, user.daily_used - n)
        db.session.add(CreditTransaction(user_id=uid, amount=n, description='Admin top-up'))
    if 'reset_password' in data:
        user.set_password(data['reset_password'])
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/admin/users/<int:uid>/delete', methods=['DELETE'])
@admin_required
def admin_delete_user(uid):
    if uid == current_user.id:
        return jsonify({'error': 'Cannot delete yourself'}), 400
    user = User.query.get_or_404(uid)
    db.session.delete(user)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/admin/plans', methods=['POST'])
@admin_required
def admin_add_plan():
    data = request.json
    plan = Plan(name=data['name'], daily_limit=int(data['daily_limit']), price=float(data.get('price', 0)))
    db.session.add(plan)
    db.session.commit()
    return jsonify({'ok': True, 'id': plan.id})


@app.route('/admin/plans/<int:pid>', methods=['PUT', 'DELETE'])
@admin_required
def admin_manage_plan(pid):
    plan = Plan.query.get_or_404(pid)
    if request.method == 'DELETE':
        db.session.delete(plan)
        db.session.commit()
        return jsonify({'ok': True})
    data = request.json
    plan.name = data.get('name', plan.name)
    plan.daily_limit = int(data.get('daily_limit', plan.daily_limit))
    plan.price = float(data.get('price', plan.price))
    db.session.commit()
    return jsonify({'ok': True})


# ── API ──────────────────────────────────────────────────────────────

@app.route('/api/rewrite', methods=['POST'])
@login_required
def api_rewrite():
    if not current_user.use_credit('AI Rewrite'):
        return jsonify({'error': 'Daily credit limit reached. Upgrade your plan.'}), 429
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        current_user.daily_used = max(0, current_user.daily_used - 1)
        db.session.commit()
        return jsonify({'error': 'API key not configured. Set ANTHROPIC_API_KEY in Railway environment.'}), 500
    data = request.json
    try:
        client = anthropic.Anthropic(api_key=api_key)
        lang = data.get('language', 'Bengali')
        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=1500,
            messages=[{'role': 'user', 'content': f"""Rewrite this news professionally in {lang}. Make it engaging and social-media ready.

Title: {data.get('title', '')}
Content: {data.get('content', '')}

Return raw JSON only:
{{"rewritten_title": "...", "rewritten_content": "...", "summary": "2-3 sentence summary"}}"""}]
        )
        text = re.sub(r'^```(?:json)?\s*\n?', '', msg.content[0].text.strip())
        text = re.sub(r'\n?```\s*$', '', text)
        return jsonify(json.loads(text.strip()))
    except Exception as e:
        current_user.daily_used = max(0, current_user.daily_used - 1)
        db.session.commit()
        return jsonify({'error': str(e)}), 500


@app.route('/api/news/<int:nid>/publish', methods=['POST'])
@login_required
def api_publish(nid):
    news = News.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    news.status = 'published'
    news.published_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/news/<int:nid>/delete', methods=['DELETE'])
@login_required
def api_delete_news(nid):
    news = News.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    db.session.delete(news)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/news/<int:nid>/card', methods=['POST'])
@login_required
def api_save_card(nid):
    news = News.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    news.card_data = request.data.decode()
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/sources', methods=['POST'])
@login_required
def api_add_source():
    data = request.json
    url = data.get('url', '').strip()
    if not url.startswith('http'):
        url = 'https://' + url
    name = data.get('name', '').strip() or url.split('/')[2].replace('www.', '')
    src = NewsSource(user_id=current_user.id, name=name, url=url)
    db.session.add(src)
    db.session.commit()
    return jsonify({'ok': True, 'id': src.id, 'name': src.name, 'url': src.url})


@app.route('/api/sources/<int:sid>', methods=['DELETE'])
@login_required
def api_delete_source(sid):
    src = NewsSource.query.filter_by(id=sid, user_id=current_user.id).first_or_404()
    db.session.delete(src)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/sources/<int:sid>/scrape', methods=['POST'])
@login_required
def api_scrape(sid):
    src = NewsSource.query.filter_by(id=sid, user_id=current_user.id).first_or_404()
    try:
        r = requests.get(src.url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        items, seen = [], set()
        for tag in soup.find_all(['article', 'h2', 'h3', 'h4'], limit=40):
            a = tag.find('a', href=True)
            if not a: continue
            title = a.get_text().strip()
            if len(title) < 20 or title in seen: continue
            seen.add(title)
            href = a['href']
            if not href.startswith('http'):
                href = urljoin(src.url, href)
            img = tag.find('img')
            img_src = img.get('src', '') if img else ''
            items.append({'title': title, 'url': href, 'image_url': img_src, 'source': src.name})
            if len(items) >= 15: break
        return jsonify({'items': items})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── INIT ─────────────────────────────────────────────────────────────

def init_db():
    db.create_all()
    if Plan.query.count() == 0:
        db.session.add_all([
            Plan(name='Free', daily_limit=5, price=0),
            Plan(name='Starter', daily_limit=20, price=9.99),
            Plan(name='Pro', daily_limit=100, price=29.99),
            Plan(name='Agency', daily_limit=500, price=99.99),
        ])
        db.session.commit()
    if User.query.count() == 0:
        admin = User(name='Admin', email='admin@newsforge.com', role='admin', plan_id=4)
        admin.set_password('admin123')
        db.session.add(admin)
        db.session.commit()
        print('\n  Default admin: admin@newsforge.com / admin123\n')


with app.app_context():
    init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_ENV') == 'development')

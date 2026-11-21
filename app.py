"""
赛事报名管理系统 - 单文件版
所有后端代码整合于此文件，适用于 Docker 打包部署
赛事名称、端口、队伍人数、初始账号等均可用环境变量覆盖（见 Config 类）
"""

import os
import sys
import uuid
import json
import secrets
import re

if __name__ == "__main__":
    sys.modules["app"] = sys.modules[__name__]
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

from flask import (Flask, Blueprint, render_template, redirect, url_for,
                   flash, request, abort, send_file, jsonify, current_app, g)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         current_user, login_required)
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFProtect
from wtforms import (StringField, PasswordField, SubmitField, TextAreaField,
                     SelectField, IntegerField, BooleanField)
from wtforms.validators import (DataRequired, Length, EqualTo, ValidationError,
                                Optional, NumberRange)
from werkzeug.security import generate_password_hash, check_password_hash
from flask_migrate import Migrate
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import contains_eager, joinedload

# 业务规则模块（积分/倍率、自动匹配）；模块内部延迟导入本文件的模型，无循环依赖
from scoring import (calculate_multiplier, get_team_rankings, get_season_summaries, qualification,
                     recalculate_all_scores, _update_team_scores, get_player_stats, player_record_query,
                     create_score_adjustment, get_member_score_breakdown)
from formal_match import (empty_record, formal_query, has_outcome, is_settled, outcome_for_player,
                          summarize_formal_matches, summarize_teams)
from matching import _auto_match
from season_safety import (SeasonError, match_scope_of, season_scope_of,
                           transition_preview, use_season_read_context, historical_context,
                           get_default_difficulty, parse_track_difficulty)
from season_health import get_season_todos
from season_phase import (admin_result_denial, admin_schedule_protected,
                          event_local, event_now, event_zone, format_event_time,
                          has_enabled_track, no_tracks_message, parse_event_time, player_match_denial,
                          registration_denial, window_warnings)

# ============================================================
# 配置
# ============================================================

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_DIR = os.path.join(BASE_DIR, '.db')


def database_uri_from_environment():
    if os.environ.get('DATABASE_URL'):
        return os.environ['DATABASE_URL']
    backend = os.environ.get('DB_BACKEND') or 'sqlite'
    if backend == 'mysql':
        from sqlalchemy.engine import URL
        return URL.create('mysql+pymysql',
            username=os.environ.get('MYSQL_USER', 'lskb'), password=os.environ.get('MYSQL_PASSWORD', ''),
            host=os.environ.get('MYSQL_HOST', 'mysql'), port=int(os.environ.get('MYSQL_PORT', '3306')),
            database=os.environ.get('MYSQL_DATABASE', 'lskb_signup'), query={'charset': 'utf8mb4'})
    if backend != 'sqlite':
        raise RuntimeError('DB_BACKEND 必须为 sqlite 或 mysql')
    path = os.environ.get('DB_FILE') or os.path.join(DB_DIR, 'lskb.db')
    return 'sqlite:///' + path.replace('\\', '/')


class Config:
    """基础配置"""
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {}
    STATE_DIR = os.environ.get('STATE_DIR', os.path.join(BASE_DIR, '.runtime'))
    BACKUP_DIR = os.environ.get('BACKUP_DIR', os.path.join(BASE_DIR, 'backups'))
    BACKUP_KEEP = 5
    UPLOAD_ROOT = os.environ.get('UPLOAD_ROOT', os.path.join(BASE_DIR, 'app', 'static', 'uploads'))
    IMAGE_DIR = os.environ.get('IMAGE_DIR', os.path.join(BASE_DIR, 'image'))
    APP_VERSION = os.environ.get('APP_VERSION') or 'p3-7'
    LEGACY_APP_VERSION = os.environ.get('LEGACY_APP_VERSION', '8238f20')
    REMEMBER_COOKIE_DURATION = timedelta(days=7)
    PERMANENT_SESSION_LIFETIME = timedelta(hours=24)
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = None
    ITEMS_PER_PAGE = 20
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024
    DEBUG = False
    TESTING = False

    # ---- 赛事可配置项（均可用环境变量覆盖，便于复用到其他赛事）----
    EVENT_NAME = os.environ.get('EVENT_NAME', '冷水坑杯')
    EVENT_TIMEZONE = os.environ.get('EVENT_TIMEZONE', 'Asia/Shanghai')
    SEASON_NUMBER = int(os.environ.get('SEASON_NUMBER', '3'))
    TOURNAMENT_NAME = os.environ.get('TOURNAMENT_NAME') or f'{EVENT_NAME} #{SEASON_NUMBER}'
    TEAM_SIZE = int(os.environ.get('TEAM_SIZE', '4'))
    TEAM_POSITIONS = [p.strip() for p in os.environ.get(
        'TEAM_POSITIONS', 'cold,normal1,normal2,pit').split(',') if p.strip()]
    # 领域术语（模板中通过 {{ labels.track }} 等使用，便于复用到非肉鸽赛事）
    TRACK_LABEL = os.environ.get('TRACK_LABEL', '肉鸽')
    MEMBER_FIELD1_LABEL = os.environ.get('MEMBER_FIELD1_LABEL', '分队')
    MEMBER_FIELD2_LABEL = os.environ.get('MEMBER_FIELD2_LABEL', '开局干员')
    PORT = int(os.environ.get('PORT', '19198'))
    # 初始账号由空赛事库的 prepare 创建。
    ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
    # 超级管理员：不设置环境变量则不创建
    SUPER_ADMIN_USERNAME = os.environ.get('SUPER_ADMIN_USERNAME', '')
    SUPER_ADMIN_PASSWORD = os.environ.get('SUPER_ADMIN_PASSWORD', '')

    # ---- 赛制默认值（换赛制时改这里或环境变量，避免散落在路由里）----
    SCHEDULE_SLOT_TIMES = [t.strip() for t in os.environ.get(
        'SCHEDULE_SLOT_TIMES', '14:00,19:00').split(',') if t.strip()]
    SCHEDULE_SLOT_MINUTES = int(os.environ.get('SCHEDULE_SLOT_MINUTES', '120'))
    DEFAULT_BONUS_SCORE = int(os.environ.get('DEFAULT_BONUS_SCORE', '500'))
    DEFAULT_DIFFICULTY_MIN = int(os.environ.get('DEFAULT_DIFFICULTY_MIN', '12'))
    DEFAULT_DIFFICULTY_MAX = int(os.environ.get('DEFAULT_DIFFICULTY_MAX', '18'))
    DEFAULT_DIFFICULTY_STEP = float(os.environ.get('DEFAULT_DIFFICULTY_STEP', '0.1'))
    # 品牌主色（模板中通过 theme.brand_color 使用）
    BRAND_PRIMARY_COLOR = os.environ.get('BRAND_PRIMARY_COLOR', '#0d6efd')


class DevelopmentConfig(Config):
    DEBUG = True
    SQLALCHEMY_ECHO = True


class TestingConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'


class ProductionConfig(Config):
    DEBUG = False


config = {
    'development': DevelopmentConfig,
    'testing': TestingConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig,
}

# ============================================================
# 扩展
# ============================================================

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()
migrate = Migrate()

# ============================================================
# 工具函数
# ============================================================


def login_required_with_role(*roles):
    """检查登录和角色权限的装饰器（管理员可访问所有页面）"""
    def decorator(func):
        @wraps(func)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                flash('请先登录', 'warning')
                return redirect(url_for('auth.login'))

            # 管理员可以访问所有页面
            if current_user.is_admin():
                return func(*args, **kwargs)

            if roles and current_user.role not in roles:
                flash('您没有权限访问此页面', 'danger')
                abort(403)

            return func(*args, **kwargs)

        return decorated_function

    return decorator


def admin_required(func):
    return login_required_with_role('admin')(func)


def captain_required(func):
    return login_required_with_role('captain')(func)


def player_required(func):
    return login_required_with_role('player')(func)


def require_status(min_status):
    """状态机守卫装饰器：要求用户 status >= min_status"""
    def decorator(func):
        @wraps(func)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                flash('请先登录', 'warning')
                return redirect(url_for('auth.login'))
            if current_user.is_admin():
                return func(*args, **kwargs)
            if current_user.status < min_status:
                flash('请先完成前置步骤', 'warning')
                if current_user.is_captain():
                    return redirect(url_for('captain.dashboard'))
                return redirect(url_for('player.dashboard'))
            return func(*args, **kwargs)
        return decorated_function
    return decorator


def set_registration_open(status):
    """设置报名状态"""
    from season_safety import SeasonError
    if status and not has_enabled_track():
        raise SeasonError(no_tracks_message())
    setting = Setting.get_instance()
    setting.registration_open = status
    db.session.commit()


def _resolve_config(config_key, env_key=None, default=None, db_getter=None):
    """三级回退读取配置：数据库设置 > app.config（环境变量）> 代码默认值"""
    if db_getter is not None:
        try:
            db_value = db_getter()
        except Exception:  # 数据库尚未就绪等情况
            db_value = None
        if db_value not in (None, ''):
            return db_value
    try:
        cfg_value = current_app.config.get(config_key)
    except RuntimeError:  # 无应用上下文（如脚本/CLI 调用）
        cfg_value = None
    if cfg_value not in (None, ''):
        return cfg_value
    if env_key:
        env_value = os.environ.get(env_key)
        if env_value not in (None, ''):
            return env_value
    return default


def get_event_name():
    from flask import g
    context = getattr(g, 'historical_context', None)
    if context:
        return context['name']
    tournament = Tournament.get_active()
    if tournament and tournament.name:
        return tournament.name
    def old_setting():
        setting = Setting.query.first()
        return setting.tournament_name if setting else None
    return _resolve_config('TOURNAMENT_NAME', 'TOURNAMENT_NAME', '赛事管理系统', old_setting)


def get_team_size():
    """队伍满员人数（含队长）：优先数据库 Setting.team_size，回退环境变量"""
    def _from_setting():
        s = Setting.query.first()
        return (s.team_size or None) if s else None

    from flask import g
    context = getattr(g, 'historical_context', None)
    if context:
        return context['team_size']
    return int(_resolve_config('TEAM_SIZE', 'TEAM_SIZE', 4, _from_setting))


def get_team_positions():
    """队内位置列表（环境变量为逗号分隔字符串）"""
    value = _resolve_config('TEAM_POSITIONS', 'TEAM_POSITIONS',
                            ['cold', 'normal1', 'normal2', 'pit'])
    if isinstance(value, str):
        return [p.strip() for p in value.split(',') if p.strip()]
    return value


def get_labels(*, use_historical=True):
    """领域术语文案（track=赛道/玩法，field1/field2=队员两个自定义字段）"""
    if use_historical and (context := getattr(g, 'historical_context', None)):
        return context['labels']
    return {
        'track': _resolve_config('TRACK_LABEL', 'TRACK_LABEL', '肉鸽'),
        'field1': _resolve_config('MEMBER_FIELD1_LABEL', 'MEMBER_FIELD1_LABEL', '分队'),
        'field2': _resolve_config('MEMBER_FIELD2_LABEL', 'MEMBER_FIELD2_LABEL', '开局干员'),
    }


# ============================================================
# 轮播媒体
# ============================================================

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
VIDEO_EXTENSIONS = {'.mp4', '.webm', '.mov'}
SUPPORTED_MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def _natural_sort_key(filename):
    """数字自然排序，使 10.png 排在 2.png 之后"""
    import re
    parts = re.split(r'(\d+)', filename)
    for i, part in enumerate(parts):
        if part.isdigit():
            parts[i] = part.zfill(20)
    return ''.join(parts)


def load_carousel_media():
    """扫描 image/ 目录，返回排序后的媒体列表（纯文件扫描，不涉及数据库）"""
    img_dir = current_app.config['IMAGE_DIR']
    if not os.path.isdir(img_dir):
        return []
    items = []
    for f in os.listdir(img_dir):
        ext = os.path.splitext(f)[1].lower()
        if ext in SUPPORTED_MEDIA_EXTENSIONS:
            items.append({
                'type': 'video' if ext in VIDEO_EXTENSIONS else 'image',
                'file': f,
            })
    items.sort(key=lambda x: _natural_sort_key(x['file']))
    return items


def _season_carousel_items(tournament):
    if tournament is None:
        return []
    return tournament.carousel_items.order_by(CarouselItem.sort_order, CarouselItem.id).all()


def _carousel_item_media_url(file_path):
    if file_path.startswith('/static/uploads/'):
        filename = file_path.removeprefix('/static/uploads/')
        endpoint = 'p0_media'
    else:
        filename = (file_path.removeprefix('/carousel-media/') if file_path.startswith('/carousel-media/')
                    else file_path.removeprefix('image/'))
        endpoint = 'main.carousel_media_serve'
    if re.search(r'[\x00-\x1f\x7f\\]', filename) or any(part in ('', '.', '..') for part in filename.split('/')):
        return None
    return url_for(endpoint, filename=filename)


def _carousel_link(link_url):
    if not link_url or link_url.startswith('//') or re.search(r'[\x00-\x1f\x7f\\]', link_url):
        return None
    try:
        parts = urlsplit(link_url)
    except ValueError:
        return None
    if parts.scheme and (parts.scheme not in ('http', 'https') or not parts.netloc):
        return None
    return link_url


def _home_carousel_media(tournament):
    items = _season_carousel_items(tournament)
    if not items:
        return [dict(item, url=url_for('main.carousel_media_serve', filename=item['file']))
                for item in load_carousel_media()]
    slides = []
    for item in items:
        if item.is_active and (url := _carousel_item_media_url(item.file_path)):
            slides.append({'type': item.media_type, 'url': url, 'title': item.title,
                           'link': _carousel_link((item.link_url or '').strip())})
    return slides


def _historical_track_name(key, tracks):
    return tracks.get(key, {}).get('name') or f'{key}（历史名称待核对）'


def get_roguelike_name(key):
    """显示对应赛季的名称，缺失记录时保留原标识以便核对。"""
    if not key:
        return key
    context = getattr(g, 'historical_context', None)
    if context:
        return _historical_track_name(key, context.get('tracks', {}))
    track = get_roguelike_by_key(key)
    return track.name if track and track.name else f'{key}（名称待核对）'


def get_match_track_name(match):
    season = match.tournament
    if season and season.is_active is False:
        tracks = historical_context(season).get('tracks', {}) if season.context_snapshot else {}
        return _historical_track_name(match.roguelike, tracks)
    return get_roguelike_name(match.roguelike)


def get_roguelike_by_key(key):
    """从数据库获取肉鸽对象"""
    return Roguelike.query.filter_by(key=key).first()


def get_active_roguelikes():
    """获取所有启用的肉鸽列表"""
    return Roguelike.query.filter_by(is_active=True).order_by(Roguelike.sort_order).all()


def get_roguelike_choices():
    """获取肉鸽下拉选项 [(key, name), ...]"""
    return [(r.key, r.name) for r in get_active_roguelikes()]


# ============================================================
# 赛事工具函数
# ============================================================


def get_tournament_status():
    """获取赛事状态"""
    tournament = Tournament.get_active()
    if tournament:
        return tournament.status
    return 'registration'


def log_system_action(action, detail=''):
    """记录系统操作审计日志"""
    try:
        user_id = current_user.id if current_user.is_authenticated else None
    except Exception:
        user_id = None
    log = SystemLog(
        user_id=user_id,
        action=action,
        detail=detail,
        ip_address=request.remote_addr if request else None
    )
    db.session.add(log)
    return log


def get_advanced_teams(rankings=None):
    """返回本届晋级队伍，包括晋级线同分队伍。"""
    if rankings is None:
        rankings = get_team_rankings()
    return qualification(rankings, Setting.get_instance().advance_count or 0)['rows']


# ============================================================
# 赛季作用域（列表/统计查询统一限定在当前赛事）
# ============================================================


def active_team_query():
    """当前赛事的队伍查询；无活跃赛事时返回空集合。"""
    tournament = Tournament.get_active()
    return Team.query.filter(Team.tournament_id == tournament.id) if tournament else Team.query.filter(db.false())


def active_match_query(*criteria):
    """当前赛事的一条正式整场一行；日程分段和表演赛不计入场次。"""
    query = formal_query(Match, db)
    tournament = Tournament.get_active()
    if tournament:
        query = query.filter(Match.tournament_id == tournament.id)
    for criterion in criteria:
        query = query.filter(criterion)
    if not tournament:
        query = query.filter(db.false())
    return query


def in_current_season(team):
    tournament = Tournament.get_active()
    return bool(team and tournament and team.tournament_id == tournament.id)


def participation_query(model, *, captain=False):
    """Keep invitation and application ownership tied to the target team."""
    owner = Team.captain_id if captain else model.user_id
    return model.query.join(Team, model.team_id == Team.id).filter(owner == current_user.id)


def current_participation(query, season):
    return query.filter(Team.tournament_id == season.id if season else db.false())


def participation_records(model, *, captain=False):
    """Render one page of owned records for the requested season scope."""
    scope = request.args.get('season', 'current')
    query = participation_query(model, captain=captain).outerjoin(Tournament, Team.tournament_id == Tournament.id)
    if scope == 'current':
        query = current_participation(query, Tournament.get_active())
    elif scope == 'history':
        query = query.filter(Tournament.is_active.is_(False), model.status.in_(('accepted', 'rejected')))
    elif scope == 'unassigned':
        query = query.filter(Tournament.id.is_(None))
    else:
        abort(400, description='season 必须是 current、history 或 unassigned')
    pager = query.options(contains_eager(model.team).contains_eager(Team.tournament), joinedload(model.user)).order_by(
        Tournament.season_number.desc(), model.created_at.desc(), model.id.desc()
    ).paginate(page=request.args.get('page', 1, type=int), per_page=20)
    return render_template('participation_records.html', records=pager.items, pagination=pager,
                           scope=scope, captain=captain, invitation=model is Invitation)


# ============================================================
# 模型 (Models)
# ============================================================


class Tournament(db.Model):
    """赛事赛季模型"""
    __tablename__ = 'tournaments'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, default='')
    season_number = db.Column(db.Integer, nullable=False, default=1, unique=True)
    is_active = db.Column(db.Boolean, default=True)
    status = db.Column(db.String(20), default='registration')  # draft, registration, matching, running, finished
    registration_start = db.Column(db.DateTime, nullable=True)
    registration_end = db.Column(db.DateTime, nullable=True)
    tournament_start = db.Column(db.DateTime, nullable=True)
    tournament_end = db.Column(db.DateTime, nullable=True)
    window_times_utc = db.Column(db.Boolean, nullable=False, server_default=db.text('0'))
    bonus_score = db.Column(db.Integer, default=500)
    context_snapshot = db.Column(db.Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    snapshot_version = db.Column(db.Integer, default=0)
    archived_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    teams = db.relationship('Team', backref='tournament', lazy='dynamic')
    matches = db.relationship('Match', backref='tournament', lazy='dynamic')
    carousel_items = db.relationship('CarouselItem', backref='tournament', lazy='dynamic', cascade='all, delete-orphan')

    @classmethod
    def get_active(cls):
        rows = cls.query.filter_by(is_active=True).limit(2).all()
        if len(rows) > 1:
            from season_safety import SeasonError
            raise SeasonError('检测到多个活跃赛季，请先按核对报告修复', 409)
        return rows[0] if rows else None

    def __repr__(self):
        return f'<Tournament {self.name}>'


class SeasonContextVersion(db.Model):
    __tablename__ = 'season_context_versions'
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)
    version = db.Column(db.Integer, nullable=False)
    context = db.Column(db.Text().with_variant(LONGTEXT(), 'mysql'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('tournament_id', 'version', name='uq_season_context_version'),)


class Roguelike(db.Model):
    """肉鸽类型模型（支持无限新增）"""
    __tablename__ = 'roguelikes'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(30), unique=True, nullable=False)
    name = db.Column(db.String(50), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    max_per_team = db.Column(db.Integer, default=1)
    sort_order = db.Column(db.Integer, default=0)
    # 难度配置（每个肉鸽独立）
    difficulty_min = db.Column(db.Integer, default=12)
    difficulty_max = db.Column(db.Integer, default=18)
    difficulty_step = db.Column(db.Float, default=0.1)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_multiplier(self, difficulty):
        """根据难度计算倍率：1 + (难度 - 最低难度) × 难度阶级"""
        return calculate_multiplier(difficulty, self.difficulty_min, self.difficulty_step)

    def __repr__(self):
        return f'<Roguelike {self.name}>'


class User(UserMixin, db.Model):
    """用户模型"""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False, index=True)
    nickname = db.Column(db.String(20), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default='player', nullable=False)
    password_changed = db.Column(db.Boolean, default=False)
    avatar = db.Column(db.String(500), nullable=True)
    status = db.Column(db.Integer, default=0)  # 0=待邀请 1=已入队 2=已选配置 3=已报时间 4=已匹配 5=比赛中 6=已提交
    has_completed_onboarding = db.Column(db.Boolean, default=False)  # 是否已完成新手引导
    tutorial_step = db.Column(db.Integer, default=0)  # 当前引导步骤（0-index），用于跨页恢复
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 一个用户可在多个赛事中分别担任队长，故为列表；请用 team 属性取当前赛事的队伍
    teams = db.relationship('Team', backref='captain', foreign_keys='Team.captain_id')
    team_memberships = db.relationship('TeamMember', backref='user', cascade='all, delete-orphan')
    invitations = db.relationship('Invitation', backref='user', cascade='all, delete-orphan')

    @property
    def team(self):
        """当前赛事下该用户作为队长的队伍（跨赛季数据共存时只取本届）"""
        tournament = Tournament.get_active()
        if tournament is None:
            return self.teams[0] if self.teams else None
        for team in self.teams:
            if team.tournament_id == tournament.id:
                return team
        return None

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def is_super_admin(self):
        return self.role == 'super_admin'

    def is_admin(self):
        return self.role in ('admin', 'super_admin')

    def is_captain(self):
        return self.role == 'captain'

    def is_player(self):
        return self.role == 'player'

    def has_team(self):
        return self.team is not None

    def get_team_membership(self):
        """获取当前赛事的队伍成员记录（队长也是队员）"""
        tournament = Tournament.get_active()
        if tournament is not None:
            for membership in self.team_memberships:
                if membership.team and membership.team.tournament_id == tournament.id:
                    return membership
            return None
        return self.team_memberships[0] if self.team_memberships else None

    def __repr__(self):
        return f'<User {self.username}>'


class Team(db.Model):
    """队伍模型"""
    __tablename__ = 'teams'

    id = db.Column(db.Integer, primary_key=True)
    team_name = db.Column(db.String(50), nullable=False, index=True)
    team_slogan = db.Column(db.String(300), default='')
    captain_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=True)
    team_photo = db.Column(db.String(500), nullable=True)
    team_score = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    members = db.relationship('TeamMember', backref='team', cascade='all, delete-orphan')
    invitations = db.relationship('Invitation', backref='team', cascade='all, delete-orphan')

    # 队名与队长唯一性按赛事隔离，同一人/同一队名可在不同赛季重复出现
    __table_args__ = (
        db.UniqueConstraint('tournament_id', 'team_name', name='uq_tournament_team_name'),
        db.UniqueConstraint('tournament_id', 'captain_id', name='uq_tournament_captain'),
    )

    def get_member_count(self):
        return len(self.members)  # 队长也是 TeamMember

    def is_full(self):
        return len(self.members) >= get_team_size()

    def __repr__(self):
        return f'<Team {self.team_name}>'


class TeamMember(db.Model):
    """队伍成员模型"""
    __tablename__ = 'team_members'

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    position = db.Column(db.String(20), nullable=True)  # cold / normal1 / normal2 / pit
    roguelike = db.Column(db.String(20), nullable=True)
    squad = db.Column(db.String(50), default='')  # 所选分队
    operator = db.Column(db.String(50), default='')  # 开局干员
    declaration = db.Column(db.String(200), default='')
    final_score = db.Column(db.Integer, default=0)  # 队员个人最终积分
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('team_id', 'user_id', name='uq_team_user'),
    )

    def __repr__(self):
        return f'<TeamMember {self.team_id}-{self.user_id}>'


class Invitation(db.Model):
    """邀请模型"""
    __tablename__ = 'invitations'

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    status = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('team_id', 'user_id', name='uq_team_invitation'),
    )

    def __repr__(self):
        return f'<Invitation {self.team_id}-{self.user_id}>'


class JoinRequest(db.Model):
    """选手申请加入队伍模型"""
    __tablename__ = 'join_requests'

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    status = db.Column(db.String(20), default='pending')  # pending/accepted/rejected
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    team = db.relationship('Team', backref=db.backref('join_requests', cascade='all, delete-orphan'))
    user = db.relationship('User', backref=db.backref('join_requests', cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('team_id', 'user_id', name='uq_team_join_request'),
    )

    def __repr__(self):
        return f'<JoinRequest {self.team_id}-{self.user_id} {self.status}>'


class Setting(db.Model):
    """系统设置模型"""
    __tablename__ = 'settings'

    id = db.Column(db.Integer, primary_key=True)
    registration_open = db.Column(db.Boolean, default=True)
    # 晋级数量
    advance_count = db.Column(db.Integer, default=0)
    # 仅用于旧库赛事名回退。当前赛事名保存在 Tournament.name。
    tournament_name = db.Column(db.String(100), default='')
    # 队伍配置（team_size 为空时回退环境变量 TEAM_SIZE）
    team_size = db.Column(db.Integer, nullable=True)
    allow_incomplete_teams = db.Column(db.Boolean, default=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @classmethod
    def get_instance(cls):
        instance = cls.query.first()
        if not instance:
            instance = cls()
            db.session.add(instance)
            db.session.flush()
        return instance

    def __repr__(self):
        return f'<Setting registration_open={self.registration_open}>'


class Match(db.Model):
    """比赛模型"""
    __tablename__ = 'matches'

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=True)
    player_a_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    player_b_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    team_a_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=True)
    team_b_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=True)
    roguelike = db.Column(db.String(20), nullable=False)
    difficulty = db.Column(db.Integer, default=12)
    player_a_difficulty = db.Column(db.Integer, nullable=True)
    player_b_difficulty = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(20), default='pending')
    player_a_multiplier = db.Column(db.Numeric(8, 4), nullable=True)
    player_b_multiplier = db.Column(db.Numeric(8, 4), nullable=True)
    scoring_rule = db.Column(db.Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    legacy_payload = db.Column(db.Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    context_version = db.Column(db.Integer, nullable=True)
    settlement_state = db.Column(db.String(20), default='pending', nullable=False)
    settled_at = db.Column(db.DateTime, nullable=True)
    forfeit_side = db.Column(db.String(1), nullable=True)
    forfeit_reason = db.Column(db.Text, default='')
    score_origin_a = db.Column(db.String(20), nullable=True)
    score_origin_b = db.Column(db.String(20), nullable=True)
    # match_type: 'match'=正式比赛 'exhibition'=表演赛
    match_type = db.Column(db.String(20), default='match')
    # parent_id: 属于哪个主比赛的子分段（NULL=独立比赛）
    parent_id = db.Column(db.Integer, db.ForeignKey('matches.id'), nullable=True)
    # segment_order: 分段排序
    segment_order = db.Column(db.Integer, default=0)
    player_a_result = db.Column(db.String(20), nullable=True)
    player_b_result = db.Column(db.String(20), nullable=True)
    player_a_score = db.Column(db.Integer, nullable=True)
    player_b_score = db.Column(db.Integer, nullable=True)
    winner_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    confirmed_a = db.Column(db.Boolean, default=False)
    confirmed_b = db.Column(db.Boolean, default=False)
    review_status = db.Column(db.String(20), default='pending')
    review_reason = db.Column(db.Text, default='')
    scheduled_time = db.Column(db.DateTime, nullable=True)
    scheduled_time_utc = db.Column(db.Boolean, nullable=False, server_default=db.text('0'))
    match_note = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)

    player_a = db.relationship('User', foreign_keys=[player_a_id], backref='matches_as_a')
    player_b = db.relationship('User', foreign_keys=[player_b_id], backref='matches_as_b')
    team_a = db.relationship('Team', foreign_keys=[team_a_id], backref='matches_as_a')
    team_b = db.relationship('Team', foreign_keys=[team_b_id], backref='matches_as_b')
    winner = db.relationship('User', foreign_keys=[winner_id], backref='matches_won')
    children = db.relationship('Match', backref=db.backref('parent', remote_side=[id]), cascade='all, delete-orphan')

    __table_args__ = (db.ForeignKeyConstraint(
        ['tournament_id', 'context_version'],
        ['season_context_versions.tournament_id', 'season_context_versions.version'],
        name='fk_match_context_version'),)

    def get_player_a_difficulty(self):
        """获取A选手难度（优先独立难度，回退统一难度）"""
        return self.player_a_difficulty if self.player_a_difficulty is not None else self.difficulty

    def get_player_b_difficulty(self):
        """获取B选手难度（优先独立难度，回退统一难度）"""
        return self.player_b_difficulty if self.player_b_difficulty is not None else self.difficulty

    def __repr__(self):
        return f'<Match {self.id}: {self.player_a_id} vs {self.player_b_id}>'


class ScoreLog(db.Model):
    """成绩修改日志"""
    __tablename__ = 'score_logs'

    id = db.Column(db.Integer, primary_key=True)
    match_id = db.Column(db.Integer, db.ForeignKey('matches.id'), nullable=False)
    modified_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    old_result = db.Column(db.Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    new_result = db.Column(db.Text().with_variant(LONGTEXT(), 'mysql'), nullable=True)
    reason = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    match = db.relationship('Match', backref='score_logs')
    modifier = db.relationship('User', backref='score_logs')

    def __repr__(self):
        return f'<ScoreLog match={self.match_id}>'


class ScoreAdjustment(db.Model):
    __tablename__ = 'score_adjustments'
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('team_members.id'), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    delta = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    revoked = db.Column(db.Boolean, default=False, nullable=False)
    revoked_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    revoke_reason = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SeasonTransition(db.Model):
    __tablename__ = 'season_transitions'
    id = db.Column(db.Integer, primary_key=True)
    request_key = db.Column(db.String(100), nullable=False, unique=True)
    expected_season_id = db.Column(db.Integer, nullable=False, unique=True)
    new_season_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    backup_batch = db.Column(db.String(100), nullable=False)
    forced_reason = db.Column(db.Text, default='')
    unfinished_ids = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class TimePreference(db.Model):
    """选手可用时间段"""
    __tablename__ = 'time_preferences'

    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    day_of_week = db.Column(db.Integer, nullable=False)  # 0=周一 ... 6=周日
    start_time = db.Column(db.String(5), nullable=False)  # "09:00"
    end_time = db.Column(db.String(5), nullable=False)    # "18:00"
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    player = db.relationship('User', backref='time_preferences')

    def __repr__(self):
        return f'<TimePreference player={self.player_id} day={self.day_of_week}>'


class CarouselItem(db.Model):
    """轮播图管理模型"""
    __tablename__ = 'carousel_items'

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=True)
    media_type = db.Column(db.String(10), default='image')  # image, video
    file_path = db.Column(db.String(500), nullable=False)
    title = db.Column(db.String(200), nullable=True)
    link_url = db.Column(db.String(500), nullable=True)
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<CarouselItem {self.title or self.file_path}>'


class SystemLog(db.Model):
    """系统日志"""
    __tablename__ = 'system_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    action = db.Column(db.String(100), nullable=False)
    detail = db.Column(db.Text().with_variant(LONGTEXT(), 'mysql'), default='')
    ip_address = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='system_logs')

    def __repr__(self):
        return f'<SystemLog {self.action}>'


class Rule(db.Model):
    """赛事规则模型（支持主规则 + 各肉鸽细分规则）"""
    __tablename__ = 'rules'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False, default='')
    roguelike_key = db.Column(db.String(30), nullable=True)  # NULL = 主规则, 否则为对应肉鸽的细分规则
    sort_order = db.Column(db.Integer, default=0)
    is_published = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<Rule {self.title}>'


# ============================================================
# 表单 (Forms)
# ============================================================


def _validate_password_strength(password):
    """验证密码强度：至少8位，包含字母和数字"""
    if len(password) < 8:
        raise ValidationError('密码至少需要8个字符')
    if not any(c.isalpha() for c in password):
        raise ValidationError('密码必须包含至少一个字母')
    if not any(c.isdigit() for c in password):
        raise ValidationError('密码必须包含至少一个数字')


class RegistrationForm(FlaskForm):
    username = StringField('用户名', validators=[
        DataRequired(message='用户名不能为空'),
        Length(min=3, max=20, message='用户名需要3-20个字符'),
    ])
    nickname = StringField('游戏昵称', validators=[
        DataRequired(message='游戏昵称不能为空'),
        Length(min=2, max=20, message='游戏昵称需要2-20个字符'),
    ])
    password = PasswordField('密码', validators=[
        DataRequired(message='密码不能为空'),
        Length(min=8, message='密码至少8个字符'),
    ])
    confirm_password = PasswordField('确认密码', validators=[
        DataRequired(message='确认密码不能为空'),
        EqualTo('password', message='两次密码输入不一致'),
    ])
    submit = SubmitField('注册')

    def validate_username(self, field):
        if User.query.filter_by(username=field.data).first():
            raise ValidationError('用户名已存在')

    def validate_nickname(self, field):
        if User.query.filter_by(nickname=field.data).first():
            raise ValidationError('游戏昵称已存在')

    def validate_password(self, field):
        _validate_password_strength(field.data)


class LoginForm(FlaskForm):
    username = StringField('用户名', validators=[DataRequired(message='用户名不能为空')])
    password = PasswordField('密码', validators=[DataRequired(message='密码不能为空')])
    submit = SubmitField('登录')


class ChangePasswordForm(FlaskForm):
    old_password = PasswordField('原密码', validators=[DataRequired(message='原密码不能为空')])
    password = PasswordField('新密码', validators=[
        DataRequired(message='新密码不能为空'),
        Length(min=8, message='密码至少8个字符'),
    ])
    confirm_password = PasswordField('确认密码', validators=[
        DataRequired(message='确认密码不能为空'),
        EqualTo('password', message='两次密码输入不一致'),
    ])
    submit = SubmitField('修改')

    def validate_password(self, field):
        _validate_password_strength(field.data)


class CreateTeamForm(FlaskForm):
    team_name = StringField('队伍名称', validators=[
        DataRequired(message='队伍名称不能为空'),
        Length(min=1, max=50, message='队伍名称1-50个字符'),
    ])
    team_slogan = TextAreaField('队伍宣言', validators=[
        Optional(),
        Length(max=300, message='队伍宣言最多300个字符'),
    ])
    submit = SubmitField('创建队伍')

    def validate_team_name(self, field):
        query = Team.query.filter_by(team_name=field.data)
        tournament = Tournament.get_active()
        if tournament:
            query = query.filter_by(tournament_id=tournament.id)
        if query.first():
            raise ValidationError('本届赛事已有同名队伍')


class EditTeamForm(FlaskForm):
    team_name = StringField('队伍名称', validators=[
        DataRequired(message='队伍名称不能为空'),
        Length(min=1, max=50, message='队伍名称1-50个字符'),
    ])
    team_slogan = TextAreaField('队伍宣言', validators=[
        Optional(),
        Length(max=300, message='队伍宣言最多300个字符'),
    ])
    submit = SubmitField('更新')


class SearchPlayerForm(FlaskForm):
    search_keyword = StringField('搜索', validators=[
        Optional(),
        Length(max=50, message='搜索关键词最多50个字符'),
    ])
    submit = SubmitField('搜索')


class InvitationForm(FlaskForm):
    player_id = StringField('选手ID', validators=[DataRequired()])
    submit = SubmitField('邀请')


class RoguelikeForm(FlaskForm):
    roguelike = SelectField(choices=[], validators=[DataRequired()])
    squad = StringField(validators=[Optional()])
    operator = StringField(validators=[Optional()])
    submit = SubmitField('确认选择')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        labels = get_labels()
        self.roguelike.label.text = f"{labels['track']}主题"
        self.squad.label.text = f"所选{labels['field1']}"
        self.operator.label.text = labels['field2']
        self.roguelike.choices = get_roguelike_choices()
        for field, label in ((self.squad, labels['field1']), (self.operator, labels['field2'])):
            length = Length(max=50, message=f'{label}名称最多50个字符')
            field.validators = [*field.validators, length]
            field.flags.maxlength = length.max


class DeclarationForm(FlaskForm):
    declaration = TextAreaField('参赛宣言', validators=[
        Optional(),
        Length(max=200, message='参赛宣言最多200个字符'),
    ])
    submit = SubmitField('更新宣言')


class RegistrationControlForm(FlaskForm):
    registration_open = SelectField('报名状态', choices=[
        ('open', '开放报名'),
        ('closed', '关闭报名'),
    ], validators=[DataRequired()])
    submit = SubmitField('更新')


class MatchScheduleForm(FlaskForm):
    """比赛时间协商表单"""
    scheduled_time = StringField('建议比赛时间', validators=[
        DataRequired(message='请输入比赛时间')
    ])
    submit = SubmitField('建议时间')


class MatchNoteForm(FlaskForm):
    """比赛留言表单"""
    match_note = TextAreaField('留言', validators=[
        Optional(),
        Length(max=500, message='留言最多500个字符')
    ])
    submit = SubmitField('发送')


class ScoreConfigForm(FlaskForm):
    """积分配置表单"""
    bonus_score = StringField('赛事加成倍率', validators=[DataRequired()])
    advance_count = StringField('晋级名额', validators=[DataRequired()])
    submit = SubmitField('更新配置')


class AdvanceCountForm(FlaskForm):
    """晋级数量表单"""
    advance_count = StringField('晋级数量', validators=[DataRequired(message='请输入晋级数量')])
    submit = SubmitField('生成晋级名单')


class TournamentSettingsForm(FlaskForm):
    """赛事设置表单"""
    tournament_name = StringField('赛事名称', validators=[Optional(), Length(max=100)])
    team_size = IntegerField('队伍满员人数', validators=[
        Optional(), NumberRange(min=1, max=20, message='队伍人数需在 1~20 之间')])
    allow_incomplete_teams = BooleanField('允许未满员队伍开赛')
    registration_start = StringField('报名开始时间', validators=[Optional()])
    registration_end = StringField('报名截止时间', validators=[Optional()])
    tournament_start = StringField('比赛开始时间', validators=[Optional()])
    tournament_end = StringField('比赛结束时间', validators=[Optional()])
    submit = SubmitField('保存设置')


class RuleForm(FlaskForm):
    """规则编辑表单"""
    title = StringField('规则标题', validators=[DataRequired(message='标题不能为空'), Length(max=200)])
    content = TextAreaField('规则内容（支持 HTML）', validators=[Optional()])
    roguelike_key = SelectField(choices=[], validators=[Optional()])
    sort_order = StringField('排序', validators=[Optional()], default='0')
    is_published = SelectField('发布状态', choices=[('1', '发布'), ('0', '隐藏')], default='1')
    submit = SubmitField('保存规则')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.roguelike_key.label.text = f"关联{get_labels()['track']}"


# ============================================================
# Blueprint 定义
# ============================================================

main_bp = Blueprint('main', __name__)
auth_bp = Blueprint('auth', __name__, url_prefix='/auth')
player_bp = Blueprint('player', __name__, url_prefix='/player')
captain_bp = Blueprint('captain', __name__, url_prefix='/captain')
admin_bp = Blueprint('admin', __name__, url_prefix='/admin')
tournament_bp = Blueprint('tournament', __name__, url_prefix='/tournament')

# ============================================================
# Main 路由
# ============================================================


@main_bp.route('/')
def index():
    setting = Setting.get_instance()
    tournament = Tournament.get_active()
    team_query = active_team_query()
    total_teams = team_query.count()
    total_members = TeamMember.query.join(Team).filter(
        Team.tournament_id == tournament.id).count() if tournament else TeamMember.query.count()
    match_counts = summarize_formal_matches(active_match_query(), Match)
    page = request.args.get('page', 1, type=int)
    latest_teams = team_query.order_by(Team.created_at.desc()).paginate(
        page=page, per_page=10)
    carousel_media = _home_carousel_media(tournament)
    # TOP10 排行榜
    rankings = get_team_rankings()[:10]
    return render_template('index.html',
                           registration_open=setting.registration_open,
                           tournament_status=get_tournament_status(),
                           tournament=tournament,
                           total_teams=total_teams,
                           total_members=total_members,
                           match_counts=match_counts,
                           latest_teams=latest_teams,
                           rankings=rankings,
                           carousel_media=carousel_media)


@main_bp.route('/carousel-media/<path:filename>')
def carousel_media_serve(filename):
    """提供轮播媒体文件（图片/视频），支持子目录"""
    if '\0' in filename:
        abort(404)
    img_dir = Path(os.path.realpath(current_app.config['IMAGE_DIR']))
    real = Path(os.path.realpath(img_dir / filename))
    if not real.is_relative_to(img_dir) or not real.is_file():
        abort(404)
    return send_file(real)


@main_bp.route('/teams')
def teams_list():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '', type=str)
    query = Team.query
    tournament = Tournament.get_active()
    if tournament:
        query = query.filter(Team.tournament_id == tournament.id)
    if search:
        query = query.filter(Team.team_name.ilike(f'%{search}%'))
    teams = query.order_by(Team.created_at.desc()).paginate(page=page, per_page=20)
    return render_template('teams_list.html', teams=teams, search=search)


@main_bp.route('/team/<int:team_id>')
def team_detail(team_id):
    team = Team.query.get_or_404(team_id)
    scope = season_scope_of(team)
    if scope == 'archived':
        return redirect(url_for('main.history_detail', tournament_id=team.tournament_id))
    if scope == 'orphan':
        use_season_read_context(None)
    members = TeamMember.query.filter_by(team_id=team.id).all()
    return render_template('team_detail_public.html', team=team, members=members, season_scope=scope)


@main_bp.route('/history')
def history():
    """往届赛事归档列表（已结束的赛季，只读）"""
    pager = Tournament.query.filter_by(is_active=False).order_by(
        Tournament.season_number.desc(), Tournament.id.desc()).paginate(
            page=request.args.get('page', 1, type=int), per_page=20)
    summaries = get_season_summaries([season.id for season in pager.items])
    items = [dict(summaries[season.id], tournament=season) for season in pager.items]
    return render_template('history/index.html', seasons=items, pagination=pager)


@main_bp.route('/history/<int:tournament_id>')
def history_detail(tournament_id):
    """往届赛事详情：冠亚季军、队伍积分榜、队员积分榜、比赛结果（只读）"""
    tournament = Tournament.query.get_or_404(tournament_id)
    if tournament.is_active:
        # 当前赛季的数据走赛事大厅/排行榜，避免出现两个入口
        return redirect(url_for('tournament.hall'))
    if tournament.is_active is None:
        raise SeasonError('该届归档状态待核对')
    use_season_read_context(tournament)
    rankings = get_team_rankings(tournament.id)
    podiums = {rank: [row for row in rankings if row['rank'] == rank] for rank in (1, 2, 3)}
    members = TeamMember.query.join(Team).filter(
        Team.tournament_id == tournament.id).order_by(
        TeamMember.final_score.desc()).all()
    match_query = formal_query(Match, db, tournament.id)
    matches = match_query.order_by(Match.created_at.desc()).all()
    quota = getattr(g, 'historical_context', {}).get('advance_count')
    return render_template('history/detail.html',
                           qualification=qualification(rankings, quota),
                           match_counts=summarize_formal_matches(match_query, Match),
                           historical_context=getattr(g, 'historical_context', {}),
                           tournament=tournament,
                           rankings=rankings,
                           members=members,
                           matches=matches,
                           champions=podiums[1], runner_ups=podiums[2], third_places=podiums[3],
                           champion=podiums[1][0] if podiums[1] else None,
                           runner_up=podiums[2][0] if podiums[2] else None,
                           third_place=podiums[3][0] if podiums[3] else None)


@main_bp.route('/rules')
def rules():
    """主规则页面 + 各肉鸽细分规则入口"""
    main_rules = Rule.query.filter_by(roguelike_key=None, is_published=True).order_by(Rule.sort_order).all()

    # 构建肉鸽细分规则导航
    all_roguelikes = get_active_roguelikes()
    roguelike_rules = []
    for rl in all_roguelikes:
        count = Rule.query.filter_by(roguelike_key=rl.key, is_published=True).count()
        roguelike_rules.append({
            'key': rl.key,
            'name': rl.name,
            'rule_count': count,
            'difficulty_min': rl.difficulty_min,
            'difficulty_max': rl.difficulty_max,
        })

    return render_template('rules.html', main_rules=main_rules, roguelike_rules=roguelike_rules)


@main_bp.route('/rules/<roguelike_key>')
def roguelike_rule(roguelike_key):
    """单个肉鸽的细分规则页面"""
    rl = get_roguelike_by_key(roguelike_key)
    if not rl:
        abort(404)

    rules = Rule.query.filter_by(roguelike_key=roguelike_key, is_published=True).order_by(Rule.sort_order).all()
    return render_template('rules_roguelike.html', roguelike=rl, rules=rules)


@main_bp.errorhandler(404)
def not_found(error):
    return render_template('404.html'), 404


@main_bp.errorhandler(500)
def internal_error(error):
    return render_template('500.html'), 500


@main_bp.errorhandler(403)
def forbidden(error):
    return render_template('403.html'), 403


# ============================================================
# Auth 路由
# ============================================================


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    denial = registration_denial(Tournament.get_active())
    if denial:
        flash(denial, 'warning')
        return redirect(url_for('main.index'))
    form = RegistrationForm()
    if form.validate_on_submit():
        user = User(username=form.username.data, nickname=form.nickname.data, role='player')
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()
        flash('注册成功！请登录', 'success')
        return redirect(url_for('auth.login'))
    return render_template('auth/register.html', form=form)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user is None or not user.check_password(form.password.data):
            flash('用户名或密码错误', 'danger')
            return redirect(url_for('auth.login'))
        login_user(user, remember=form.username.data)
        if user.is_admin():
            return redirect(url_for('admin.dashboard'))
        elif user.is_captain():
            return redirect(url_for('captain.dashboard'))
        else:
            return redirect(url_for('player.dashboard'))
    return render_template('auth/login.html', form=form)


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('已登出', 'info')
    return redirect(url_for('main.index'))


@auth_bp.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.old_password.data):
            flash('原密码错误', 'danger')
            return redirect(url_for('auth.change_password'))
        current_user.set_password(form.password.data)
        current_user.password_changed = True
        db.session.commit()
        flash('密码已修改', 'success')
        if current_user.is_admin():
            return redirect(url_for('admin.dashboard'))
        elif current_user.is_captain():
            return redirect(url_for('captain.dashboard'))
        else:
            return redirect(url_for('player.dashboard'))
    return render_template('auth/change_password.html', form=form)


# ============================================================
# Player 路由
# ============================================================


@player_bp.route('/dashboard')
@login_required_with_role('player', 'captain')
def dashboard():
    tournament = Tournament.get_active()
    invitations = current_participation(participation_query(Invitation), tournament).filter(
        Invitation.status == 'pending').all()
    team_membership = current_user.get_team_membership() if tournament else None
    team = team_membership.team if team_membership else None
    return render_template('player/dashboard.html',
                           invitations=invitations,
                           team=team,
                           team_membership=team_membership)


@player_bp.route('/invitations')
@login_required_with_role('player', 'captain')
def invitations():
    return participation_records(Invitation)


@player_bp.route('/invitation/<int:invitation_id>/accept', methods=['POST'])
@login_required_with_role('player', 'captain')
def accept_invitation(invitation_id):
    invitation = Invitation.query.get_or_404(invitation_id)
    if invitation.status != 'pending':
        flash('邀请已处理', 'warning')
        return redirect(url_for('player.invitations'))
    # 检查是否已在其他队伍中
    if current_user.has_team() or current_user.get_team_membership():
        flash('您已在其他队伍中，不能接受新邀请', 'warning')
        invitation.status = 'rejected'
        db.session.commit()
        return redirect(url_for('player.invitations'))
    team = invitation.team
    if team.is_full():
        flash('队伍已满员', 'danger')
        invitation.status = 'rejected'
        db.session.commit()
        return redirect(url_for('player.invitations'))
    invitation.status = 'accepted'
    team_member = TeamMember(team_id=team.id, user_id=current_user.id)
    db.session.add(team_member)
    if not current_user.is_admin():
        current_user.role = 'player'  # 管理员入队保留原角色
    current_user.status = 1  # 已入队
    db.session.commit()
    flash(f'成功加入队伍 【{team.team_name}】', 'success')
    return redirect(url_for('player.dashboard'))


@player_bp.route('/invitation/<int:invitation_id>/reject', methods=['POST'])
@login_required_with_role('player', 'captain')
def reject_invitation(invitation_id):
    invitation = Invitation.query.get_or_404(invitation_id)
    if invitation.status != 'pending':
        flash('邀请已处理', 'warning')
        return redirect(url_for('player.invitations'))
    invitation.status = 'rejected'
    db.session.commit()
    flash('已拒绝邀请', 'info')
    return redirect(url_for('player.invitations'))


@player_bp.route('/team/<int:team_id>')
@login_required_with_role('player', 'captain')
def team_detail(team_id):
    team = Team.query.get_or_404(team_id)
    scope = season_scope_of(team)
    if scope == 'archived':
        return redirect(url_for('main.history_detail', tournament_id=team.tournament_id))
    if scope == 'orphan':
        return redirect(url_for('main.team_detail', team_id=team.id))
    team_membership = current_user.get_team_membership()
    if not team_membership or team_membership.team_id != team_id:
        flash('您不在此队伍中', 'warning')
        return redirect(url_for('player.dashboard'))
    return render_template('player/team_detail.html', team=team, team_membership=team_membership)


@player_bp.route('/team/roguelike', methods=['GET', 'POST'])
@login_required_with_role('player', 'captain')
def select_roguelike():
    team_membership = current_user.get_team_membership()
    if not team_membership:
        flash('您还未加入任何队伍', 'warning')
        if current_user.role == 'captain':
            return redirect(url_for('captain.dashboard'))
        return redirect(url_for('player.dashboard'))

    denial = registration_denial(Tournament.get_active())
    if denial:
        flash(denial, 'warning')
        return redirect(url_for('player.dashboard'))
    team_id = team_membership.team_id

    # 统计队伍中每个肉鸽的已选人数
    team_counts = {}
    counts = db.session.query(
        TeamMember.roguelike, db.func.count(TeamMember.id)
    ).filter(
        TeamMember.team_id == team_id,
        TeamMember.roguelike.isnot(None)
    ).group_by(TeamMember.roguelike).all()
    for key, cnt in counts:
        team_counts[key] = cnt

    # 获取所有启用的肉鸽及其 team_limit
    all_roguelikes = get_active_roguelikes()
    roguelike_limits = {r.key: r.max_per_team for r in all_roguelikes}

    form = RoguelikeForm()
    # 动态过滤：已达上限的肉鸽不可选（保留自己当前选的）
    form.roguelike.choices = [
        (r.key, f"{r.name} ({team_counts.get(r.key, 0)}/{r.max_per_team})")
        for r in all_roguelikes
        if team_counts.get(r.key, 0) < r.max_per_team or r.key == team_membership.roguelike
    ]

    # GET 请求时预填已有数据
    if request.method == 'GET':
        if team_membership.roguelike:
            form.roguelike.data = team_membership.roguelike
        form.squad.data = team_membership.squad or ''
        form.operator.data = team_membership.operator or ''

    if form.validate_on_submit():
        track_label = get_labels()['track']
        chosen = form.roguelike.data
        # 检查 team_limit
        current_count = team_counts.get(chosen, 0)
        limit = roguelike_limits.get(chosen, 1)
        if chosen != team_membership.roguelike and current_count >= limit:
            flash(f'该{track_label}已达到队伍选择上限（{limit}人）', 'danger')
            return redirect(url_for('player.select_roguelike'))
        team_membership.roguelike = chosen
        team_membership.squad = (form.squad.data or '').strip()
        team_membership.operator = (form.operator.data or '').strip()
        if current_user.status < 2:
            current_user.status = 2  # 已选配置
        db.session.commit()
        flash(f'已选择{track_label}：{get_roguelike_name(chosen)}', 'success')
        if current_user.role == 'captain':
            return redirect(url_for('captain.dashboard'))
        return redirect(url_for('player.team_detail', team_id=team_membership.team_id))

    # 获取已被队友选择的肉鸽列表
    taken_roguelikes = [k for k, v in team_counts.items() if v > 0 and k != team_membership.roguelike]

    return render_template('player/select_roguelike.html',
                           form=form,
                           roguelike=team_membership.roguelike,
                           roguelike_limits=roguelike_limits,
                           team_counts=team_counts,
                           taken_roguelikes=taken_roguelikes)


@player_bp.route('/declaration', methods=['GET', 'POST'])
@login_required_with_role('player', 'captain')
def declaration():
    team_membership = current_user.get_team_membership()
    if not team_membership:
        flash('您还未加入任何队伍', 'warning')
        if current_user.role == 'captain':
            return redirect(url_for('captain.dashboard'))
        return redirect(url_for('player.dashboard'))
    form = DeclarationForm()
    if form.validate_on_submit():
        team_membership.declaration = form.declaration.data
        db.session.commit()
        flash('参赛宣言已更新', 'success')
        if current_user.role == 'captain':
            return redirect(url_for('captain.dashboard'))
        return redirect(url_for('player.team_detail', team_id=team_membership.team_id))
    elif request.method == 'GET':
        form.declaration.data = team_membership.declaration
    return render_template('player/declaration.html', form=form)


def requested_record_scope():
    scope = request.args.get('season', 'current').strip().lower()
    if scope not in ('current', 'all'):
        abort(400, description='season 必须是 current 或 all')
    return scope


@player_bp.route('/profile', methods=['GET', 'POST'])
@login_required_with_role('player', 'captain')
def profile():
    scope = requested_record_scope()
    team_membership = current_user.get_team_membership() if Tournament.get_active() else None
    form = DeclarationForm()
    if form.validate_on_submit():
        if not team_membership:
            flash('加入队伍后才能填写参赛宣言', 'warning')
            return redirect(url_for('player.profile'))
        team_membership.declaration = (form.declaration.data or '').strip()
        db.session.commit()
        flash('参赛宣言已更新', 'success')
        return redirect(url_for('player.profile', season=scope))
    if request.method == 'GET' and team_membership:
        form.declaration.data = team_membership.declaration or ''
    stats = get_player_stats(current_user.id, scope=scope, recent_limit=10)
    return render_template('player/profile.html',
                           team_membership=team_membership,
                           stats=stats,
                           form=form)


@player_bp.route('/upload-avatar', methods=['POST'])
@login_required_with_role('player', 'captain')
def upload_avatar():
    if 'avatar' not in request.files:
        flash('请选择文件', 'warning')
        return redirect(url_for('player.profile'))
    file = request.files['avatar']
    if file.filename == '':
        flash('请选择文件', 'warning')
        return redirect(url_for('player.profile'))
    allowed_ext = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in allowed_ext:
        flash('仅支持 PNG/JPG/GIF/WEBP 格式', 'danger')
        return redirect(url_for('player.profile'))
    # 生成唯一文件名
    filename = f'{uuid.uuid4().hex}.{ext}'
    filepath = os.path.join(current_app.config['UPLOAD_ROOT'], 'avatars', filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    try:
        file.save(filepath)
    except Exception as e:
        flash(f'头像保存失败: {str(e)}', 'danger')
        return redirect(url_for('player.profile'))
    current_user.avatar = filename
    db.session.commit()
    flash('头像上传成功', 'success')
    return redirect(url_for('player.profile'))


@player_bp.route('/time-preferences', methods=['GET', 'POST'])
@login_required_with_role('player', 'captain')
def time_preferences():
    """选手填写可用比赛时间"""
    if not current_user.is_admin() and current_user.status < 2:
        flash(f"请先选择{get_labels()['track']}和填写宣言", 'warning')
        if current_user.is_captain():
            return redirect(url_for('captain.dashboard'))
        return redirect(url_for('player.dashboard'))

    if request.method == 'POST':
        # 清除旧时间段
        TimePreference.query.filter_by(player_id=current_user.id).delete()
        # 读取新时间段
        days = request.form.getlist('day_of_week')
        starts = request.form.getlist('start_time')
        ends = request.form.getlist('end_time')
        count = 0
        for i in range(len(days)):
            if i < len(starts) and i < len(ends) and days[i] and starts[i] and ends[i]:
                try:
                    day = int(days[i])
                    tp = TimePreference(
                        player_id=current_user.id,
                        day_of_week=day,
                        start_time=starts[i],
                        end_time=ends[i]
                    )
                    db.session.add(tp)
                    count += 1
                except (ValueError, TypeError):
                    pass
        if count > 0:
            current_user.status = 3  # 已报时间
        db.session.commit()
        flash(f'已保存 {count} 个可用时间段', 'success')
        if current_user.is_captain():
            return redirect(url_for('captain.dashboard'))
        return redirect(url_for('player.dashboard'))

    # GET: 显示表单
    existing = TimePreference.query.filter_by(player_id=current_user.id).all()
    day_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    return render_template('player/time_preferences.html',
                           existing=existing,
                           day_names=day_names)


# ============================================================
# 选手申请加入队伍
# ============================================================


@player_bp.route('/join-request/search', methods=['GET', 'POST'])
@login_required_with_role('player', 'captain')
def search_teams():
    """选手搜索队伍并申请加入"""
    tournament = Tournament.get_active()
    if tournament and (current_user.has_team() or current_user.get_team_membership()):
        flash('您已在队伍中，不能申请加入其他队伍', 'warning')
        return redirect(url_for('player.dashboard'))
    form = SearchPlayerForm()
    teams = []
    if form.validate_on_submit():
        keyword = form.search_keyword.data
        query = active_team_query().filter(Team.team_name.ilike(f'%{keyword}%'))
        for team in query.all():
            if not team.is_full():
                # 检查是否已有待处理申请
                existing = JoinRequest.query.filter_by(
                    team_id=team.id, user_id=current_user.id, status='pending'
                ).first()
                team._has_pending = existing is not None
                teams.append(team)
    return render_template('player/search_teams.html',
                           form=form, teams=teams)


@player_bp.route('/join-request/<int:team_id>', methods=['POST'])
@login_required_with_role('player', 'captain')
def request_join(team_id):
    """选手申请加入队伍"""
    if current_user.has_team() or current_user.get_team_membership():
        flash('您已在队伍中', 'warning')
        return redirect(url_for('player.dashboard'))
    team = Team.query.get_or_404(team_id)
    if team.is_full():
        flash('队伍已满员', 'danger')
        return redirect(url_for('player.search_teams'))
    existing = JoinRequest.query.filter_by(
        team_id=team.id, user_id=current_user.id
    ).first()
    if existing:
        if existing.status == 'pending':
            flash('您已向该队伍发送申请，请等待队长审核', 'warning')
        else:
            existing.status = 'pending'
            existing.updated_at = datetime.utcnow()
            db.session.commit()
            flash(f'已重新向队伍「{team.team_name}」发送申请', 'success')
    else:
        join_req = JoinRequest(team_id=team.id, user_id=current_user.id)
        db.session.add(join_req)
        db.session.commit()
        flash(f'已向队伍「{team.team_name}」发送加入申请', 'success')
    return redirect(url_for('player.search_teams'))


@player_bp.route('/my-join-requests')
@login_required_with_role('player', 'captain')
def my_join_requests():
    return participation_records(JoinRequest)


# ============================================================
# 队长审核加入申请
# ============================================================


@captain_bp.route('/join-requests')
@login_required_with_role('captain')
def join_requests():
    return participation_records(JoinRequest, captain=True)


@captain_bp.route('/join-request/<int:request_id>/accept', methods=['POST'])
@login_required_with_role('captain')
def accept_join_request(request_id):
    """队长接受加入申请"""
    join_req = JoinRequest.query.get_or_404(request_id)
    team = join_req.team
    if join_req.status != 'pending':
        flash('该申请已处理', 'warning')
        return redirect(url_for('captain.join_requests'))
    if team.is_full():
        flash('队伍已满员', 'danger')
        join_req.status = 'rejected'
        db.session.commit()
        return redirect(url_for('captain.join_requests'))
    player = join_req.user
    if player.has_team() or player.get_team_membership():
        flash('该选手已在其他队伍中', 'warning')
        join_req.status = 'rejected'
        db.session.commit()
        return redirect(url_for('captain.join_requests'))
    join_req.status = 'accepted'
    team_member = TeamMember(team_id=team.id, user_id=player.id)
    db.session.add(team_member)
    player.status = 1  # 已入队
    db.session.commit()
    flash(f'已接受 {player.nickname} 的加入申请', 'success')
    return redirect(url_for('captain.join_requests'))


@captain_bp.route('/join-request/<int:request_id>/reject', methods=['POST'])
@login_required_with_role('captain')
def reject_join_request(request_id):
    """队长拒绝加入申请"""
    join_req = JoinRequest.query.get_or_404(request_id)
    if join_req.status != 'pending':
        flash('该申请已处理', 'warning')
        return redirect(url_for('captain.join_requests'))
    join_req.status = 'rejected'
    db.session.commit()
    flash(f'已拒绝 {join_req.user.nickname} 的加入申请', 'info')
    return redirect(url_for('captain.join_requests'))


# ============================================================
# 新手引导 API
# ============================================================


@player_bp.route('/api/onboarding/complete', methods=['POST'])
@login_required
def api_complete_onboarding():
    """标记新手引导已完成"""
    current_user.has_completed_onboarding = True
    current_user.tutorial_step = 0
    db.session.commit()
    return jsonify({'success': True})


@player_bp.route('/api/onboarding/reset', methods=['POST'])
@login_required
def api_reset_onboarding():
    """重置新手引导状态（允许再次查看）"""
    current_user.has_completed_onboarding = False
    current_user.tutorial_step = 0
    db.session.commit()
    return jsonify({'success': True})


@player_bp.route('/api/onboarding/status')
@login_required
def api_onboarding_status():
    """获取新手引导状态"""
    return jsonify({
        'has_completed_onboarding': current_user.has_completed_onboarding,
        'tutorial_step': current_user.tutorial_step,
        'role': current_user.role,
        'nickname': current_user.nickname,
    })


# ============================================================
# 选手数据 API
# ============================================================


@player_bp.route('/api/players')
@login_required
def api_players():
    """获取所有选手数据（JSON）—— 包括队长"""
    players = User.query.filter(User.role.in_(['player', 'captain'])).all()
    result = []
    for p in players:
        tm = p.get_team_membership()
        result.append({
            'id': p.id,
            'username': p.username,
            'nickname': p.nickname,
            'avatar': url_for('static', filename='uploads/avatars/' + p.avatar, _external=True) if p.avatar else None,
            'role': p.role,
            'created_at': p.created_at.isoformat() if p.created_at else None,
            'team': {
                'id': tm.team_id if tm else None,
                'name': tm.team.team_name if tm else None,
                'team_photo': url_for('static', filename='uploads/teams/' + tm.team.team_photo, _external=True) if (tm and tm.team and tm.team.team_photo) else None,
                'roguelike': tm.roguelike if tm else None,
                'squad': tm.squad or '' if tm else '',
                'operator': tm.operator or '' if tm else '',
                'final_score': tm.final_score if tm else 0,
            } if tm else None,
        })
    return jsonify({'players': result, 'total': len(result)})


def player_detail_response(player):
    scope = requested_record_scope()
    membership = player.get_team_membership() if Tournament.get_active() else None
    stats = get_player_stats(player.id, scope=scope, recent_limit=20)
    matches = []
    for match in stats.pop('matches'):
        opponent = match.player_b if match.player_a_id == player.id else match.player_a
        matches.append({
            'id': match.id, 'roguelike': match.roguelike, 'difficulty': match.difficulty,
            'opponent': {'id': opponent.id, 'nickname': opponent.nickname} if opponent else None,
            'result': outcome_for_player(match, player.id) or match.status,
            'status': match.status, 'settlement_state': match.settlement_state,
            'created_at': match.created_at.isoformat() if match.created_at else None,
            'season_id': match.tournament_id, 'season_number': match.tournament.season_number,
        })
    return jsonify({
        'player': {
            'id': player.id,
            'username': player.username,
            'nickname': player.nickname,
            'avatar': url_for('static', filename='uploads/avatars/' + player.avatar, _external=True) if player.avatar else None,
            'role': player.role,
            'created_at': player.created_at.isoformat() if player.created_at else None,
            'team': {
                'id': membership.team_id,
                'name': membership.team.team_name,
                'team_photo': url_for('static', filename='uploads/teams/' + membership.team.team_photo, _external=True)
                if membership.team.team_photo else None,
                'roguelike': membership.roguelike,
                'squad': membership.squad or '',
                'operator': membership.operator or '',
                'declaration': membership.declaration,
                'final_score': membership.final_score,
            } if membership else None,
        },
        'stats': stats,
        'recent_matches': matches,
    })


@player_bp.route('/api/players/<int:user_id>')
@login_required
def api_player_detail(user_id):
    return player_detail_response(User.query.get_or_404(user_id))


@player_bp.route('/api/players/<name>')
@login_required
def api_player_by_name(name):
    player = User.query.filter_by(nickname=name).first()
    if player is None:
        player = User.query.filter_by(username=name).first_or_404()
    return player_detail_response(player)


# ============================================================
# Captain 路由
# ============================================================


@captain_bp.route('/dashboard')
@login_required_with_role('captain')
def dashboard():
    tournament = Tournament.get_active()
    team = current_user.team if tournament else None
    if not team:
        return render_template('captain/dashboard.html', team=None)
    members = TeamMember.query.filter_by(team_id=team.id).all()
    pending_invitations = current_participation(participation_query(Invitation, captain=True), tournament).filter(
        Invitation.status == 'pending').all()
    # 队长自己的参赛信息
    my_membership = TeamMember.query.filter_by(
        team_id=team.id, user_id=current_user.id).first()
    return render_template('captain/dashboard.html',
                           team=team, members=members,
                           pending_invitations=pending_invitations,
                           my_membership=my_membership)


@captain_bp.route('/team/create', methods=['GET', 'POST'])
@login_required_with_role('captain')
def create_team():
    denial = registration_denial(Tournament.get_active())
    if denial:
        flash(denial, 'warning')
        return redirect(url_for('captain.dashboard'))
    if current_user.has_team():
        flash('您已创建队伍，不能创建多个队伍', 'warning')
        return redirect(url_for('captain.dashboard'))
    form = CreateTeamForm()
    if form.validate_on_submit():
        tournament = Tournament.get_active()
        team = Team(team_name=form.team_name.data,
                    team_slogan=form.team_slogan.data,
                    captain_id=current_user.id,
                    tournament_id=tournament.id if tournament else None)
        db.session.add(team)
        db.session.flush()  # 获取 team.id
        # 队长自动成为队员
        captain_member = TeamMember(team_id=team.id, user_id=current_user.id)
        db.session.add(captain_member)
        db.session.commit()
        flash(f"队伍 【{team.team_name}】 创建成功！您已自动成为队员，请选择{get_labels()['track']}和位置", 'success')
        return redirect(url_for('captain.dashboard'))
    return render_template('captain/create_team.html', form=form)


@captain_bp.route('/team/edit', methods=['GET', 'POST'])
@login_required_with_role('captain')
def edit_team():
    team = current_user.team
    if not team:
        flash('您还未创建队伍', 'warning')
        return redirect(url_for('captain.dashboard'))
    form = EditTeamForm()
    if form.validate_on_submit():
        existing = Team.query.filter(
            Team.team_name == form.team_name.data,
            Team.tournament_id == team.tournament_id,
            Team.id != team.id).first()
        if existing:
            flash('本届赛事已有同名队伍', 'danger')
            return redirect(url_for('captain.edit_team'))
        team.team_name = form.team_name.data
        team.team_slogan = form.team_slogan.data
        db.session.commit()
        flash('队伍信息已更新', 'success')
        return redirect(url_for('captain.dashboard'))
    elif request.method == 'GET':
        form.team_name.data = team.team_name
        form.team_slogan.data = team.team_slogan
    return render_template('captain/edit_team.html', form=form, team=team)


@captain_bp.route('/team/members')
@login_required_with_role('captain')
def members():
    team = current_user.team
    if not team:
        flash('您还未创建队伍', 'warning')
        return redirect(url_for('captain.dashboard'))
    members = TeamMember.query.filter_by(team_id=team.id).all()
    return render_template('captain/members.html', team=team, members=members)


@captain_bp.route('/team/assign-position', methods=['POST'])
@login_required_with_role('captain')
def assign_position():
    """队长为队员分配位置"""
    team = current_user.team
    if not team:
        flash('您还未创建队伍', 'warning')
        return redirect(url_for('captain.dashboard'))
    member_id = request.form.get('member_id', type=int)
    position = request.form.get('position', '').strip()
    valid_positions = get_team_positions()
    if position not in valid_positions:
        flash('无效的位置', 'danger')
        return redirect(url_for('captain.members'))
    member = TeamMember.query.get_or_404(member_id)
    if member.team_id != team.id:
        flash('该成员不属于您的队伍', 'danger')
        return redirect(url_for('captain.members'))
    # 检查该位置是否已被占用
    existing = TeamMember.query.filter_by(
        team_id=team.id, position=position
    ).first()
    if existing and existing.id != member.id:
        # 清除旧占用者的位置
        existing.position = None
    member.position = position
    db.session.commit()
    flash(f'已将 {member.user.nickname} 分配到 {position} 位', 'success')
    return redirect(url_for('captain.members'))


@captain_bp.route('/invite/search', methods=['GET', 'POST'])
@login_required_with_role('captain')
def search_players():
    team = active_team_query().filter_by(captain_id=current_user.id).first()
    if not team:
        flash('您还未创建队伍', 'warning')
        return redirect(url_for('captain.dashboard'))
    if team.is_full():
        flash('队伍已满员，不能再邀请成员', 'warning')
        return redirect(url_for('captain.dashboard'))
    denial = registration_denial(Tournament.get_active())
    if denial:
        flash(denial, 'warning')
        return redirect(url_for('captain.dashboard'))
    form = SearchPlayerForm()
    players = []
    if form.validate_on_submit():
        keyword = form.search_keyword.data
        query = User.query.filter(
            User.role.in_(['player', 'captain', 'admin', 'super_admin']),
            db.or_(
                User.username.ilike(f'%{keyword}%'),
                User.nickname.ilike(f'%{keyword}%')))
        valid_players = []
        for player in query.all():
            if player.id == current_user.id:
                continue
            # 检查是否已在任何队伍中（作为队长或队员）
            if player.has_team() or player.get_team_membership():
                continue
            if Invitation.query.filter_by(team_id=team.id, user_id=player.id, status='pending').first():
                continue
            valid_players.append(player)
        players = valid_players
    return render_template('captain/search_players.html',
                           form=form, players=players, team=team)


@captain_bp.route('/invite/<int:player_id>', methods=['POST'])
@login_required_with_role('captain')
def invite_player(player_id):
    team = active_team_query().filter_by(captain_id=current_user.id).first()
    if not team:
        flash('您还未创建队伍', 'warning')
        return redirect(url_for('captain.dashboard'))
    if team.is_full():
        flash('队伍已满员', 'danger')
        return redirect(url_for('captain.search_players'))
    player = User.query.get_or_404(player_id)
    if player.id == current_user.id:
        flash('不能邀请自己', 'warning')
        return redirect(url_for('captain.search_players'))
    if player.has_team() or player.get_team_membership():
        flash('该选手已在其他队伍中', 'warning')
        return redirect(url_for('captain.search_players'))
    existing = Invitation.query.filter_by(team_id=team.id, user_id=player_id).first()
    if existing:
        if existing.status == 'pending':
            flash('已向该选手发送邀请，请等待回复', 'warning')
            return redirect(url_for('captain.search_players'))
        existing.status = 'pending'
        existing.updated_at = datetime.utcnow()
        db.session.commit()
        flash(f'已重新向 【{player.nickname}】 发送邀请', 'success')
    else:
        invitation = Invitation(team_id=team.id, user_id=player_id, status='pending')
        db.session.add(invitation)
        db.session.commit()
        flash(f'已向 【{player.nickname}】 发送邀请', 'success')
    return redirect(url_for('captain.search_players'))


@captain_bp.route('/invitations')
@login_required_with_role('captain')
def invitations():
    return participation_records(Invitation, captain=True)


@captain_bp.route('/invitation/<int:invitation_id>/cancel', methods=['POST'])
@login_required_with_role('captain')
def cancel_invitation(invitation_id):
    invitation = Invitation.query.get_or_404(invitation_id)
    if invitation.status != 'pending':
        flash('只能取消待处理邀请', 'warning')
        return redirect(url_for('captain.invitations'))
    user = invitation.user
    db.session.delete(invitation)
    db.session.commit()
    flash(f'已取消向 【{user.nickname}】 的邀请', 'info')
    return redirect(url_for('captain.invitations'))


@captain_bp.route('/team/info')
@login_required_with_role('captain')
def team_info():
    team = current_user.team
    if not team:
        flash('您还未创建队伍', 'warning')
        return redirect(url_for('captain.dashboard'))
    members = TeamMember.query.filter_by(team_id=team.id).all()
    return render_template('captain/team_info.html', team=team, members=members)


@captain_bp.route('/profile')
@login_required_with_role('captain')
def profile():
    scope = requested_record_scope()
    team = current_user.team if Tournament.get_active() else None
    membership = TeamMember.query.filter_by(team_id=team.id, user_id=current_user.id).first() if team else None
    stats = get_player_stats(current_user.id, scope=scope, recent_limit=10)
    return render_template('captain/profile.html', team=team, my_membership=membership,
                           member_score=membership.final_score if membership else 0, stats=stats)


@captain_bp.route('/upload-avatar', methods=['POST'])
@login_required_with_role('captain')
def upload_avatar():
    if 'avatar' not in request.files:
        flash('请选择文件', 'warning')
        return redirect(url_for('captain.dashboard'))
    file = request.files['avatar']
    if file.filename == '':
        flash('请选择文件', 'warning')
        return redirect(url_for('captain.dashboard'))
    allowed_ext = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in allowed_ext:
        flash('仅支持 PNG/JPG/GIF/WEBP 格式', 'danger')
        return redirect(url_for('captain.dashboard'))
    # 生成唯一文件名
    filename = f'{uuid.uuid4().hex}.{ext}'
    filepath = os.path.join(current_app.config['UPLOAD_ROOT'], 'avatars', filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    try:
        file.save(filepath)
    except Exception as e:
        flash(f'头像保存失败: {str(e)}', 'danger')
        return redirect(url_for('captain.dashboard'))
    current_user.avatar = filename
    db.session.commit()
    flash('头像上传成功', 'success')
    return redirect(url_for('captain.dashboard'))


@captain_bp.route('/upload-team-avatar', methods=['POST'])
@login_required_with_role('captain')
def upload_team_avatar():
    """上传队伍头像"""
    team = current_user.team
    if not team:
        flash('您还未创建队伍', 'warning')
        return redirect(url_for('captain.dashboard'))
    if 'team_avatar' not in request.files:
        flash('请选择文件', 'warning')
        return redirect(url_for('captain.dashboard'))
    file = request.files['team_avatar']
    if file.filename == '':
        flash('请选择文件', 'warning')
        return redirect(url_for('captain.dashboard'))
    allowed_ext = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in allowed_ext:
        flash('仅支持 PNG/JPG/GIF/WEBP 格式', 'danger')
        return redirect(url_for('captain.dashboard'))
    filename = f'team_{uuid.uuid4().hex}.{ext}'
    filepath = os.path.join(current_app.config['UPLOAD_ROOT'], 'teams', filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    try:
        file.save(filepath)
    except Exception as e:
        flash(f'头像保存失败: {str(e)}', 'danger')
        return redirect(url_for('captain.dashboard'))
    team.team_photo = filename
    db.session.commit()
    flash('队伍头像上传成功', 'success')
    return redirect(url_for('captain.dashboard'))


# ============================================================
# 队伍数据 API（公开）
# ============================================================

@main_bp.route('/api/teams')
def api_teams():
    """获取当前赛事所有队伍数据（JSON）"""
    teams = active_team_query().order_by(Team.created_at.desc()).all()
    result = []
    for t in teams:
        members = TeamMember.query.filter_by(team_id=t.id).all()
        result.append({
            'id': t.id,
            'archived': bool(t.tournament and not t.tournament.is_active),
            'season_number': t.tournament.season_number if t.tournament else None,
            'name': t.team_name,
            'slogan': t.team_slogan,
            'team_photo': url_for('static', filename='uploads/teams/' + t.team_photo, _external=True) if t.team_photo else None,
            'captain': {'id': t.captain.id, 'nickname': t.captain.nickname} if t.captain else None,
            'member_count': len(members),
            'score': t.team_score,
            'created_at': t.created_at.isoformat() if t.created_at else None,
            'members': [{
                'id': m.user.id,
                'nickname': m.user.nickname,
                'avatar': url_for('static', filename='uploads/avatars/' + m.user.avatar, _external=True) if m.user.avatar else None,
                'roguelike': m.roguelike,
                'position': m.position,
                'declaration': m.declaration,
                'final_score': m.final_score,
            } for m in members]
        })
    return jsonify({'teams': result, 'total': len(result)})


@main_bp.route('/api/teams/<int:team_id>')
def api_team_detail(team_id):
    """获取单个队伍详细数据（JSON）"""
    t = Team.query.get_or_404(team_id)
    scope = season_scope_of(t)
    members = TeamMember.query.filter_by(team_id=t.id).all()
    return jsonify({
        'team': {
            'id': t.id,
            'archived': scope == 'archived',
            'season_scope': scope,
            'read_only': scope != 'current',
            'season_number': t.tournament.season_number if scope != 'orphan' else None,
            'name': t.team_name,
            'slogan': t.team_slogan,
            'team_photo': url_for('static', filename='uploads/teams/' + t.team_photo, _external=True) if t.team_photo else None,
            'captain': {'id': t.captain.id, 'nickname': t.captain.nickname} if t.captain else None,
            'member_count': len(members),
            'score': t.team_score,
            'created_at': t.created_at.isoformat() if t.created_at else None,
            'members': [{
                'id': m.user.id,
                'nickname': m.user.nickname,
                'avatar': url_for('static', filename='uploads/avatars/' + m.user.avatar, _external=True) if m.user.avatar else None,
                'roguelike': m.roguelike,
                'position': m.position,
                'declaration': m.declaration,
                'final_score': m.final_score,
            } for m in members]
        }
    })


# ============================================================
# Admin 路由
# ============================================================


@admin_bp.route('/dashboard')
@login_required_with_role('admin')
def dashboard():
    tournament = Tournament.get_active()
    team_query = Team.query.filter_by(tournament_id=tournament.id) if tournament else Team.query.filter(db.false())
    total_users = User.query.count()
    total_teams = team_query.count()
    total_captains = User.query.filter_by(role='captain').count()
    total_players = User.query.filter_by(role='player').count()
    total_members = TeamMember.query.join(Team).filter(
        Team.tournament_id == tournament.id).count() if tournament else TeamMember.query.count()
    latest_teams = team_query.order_by(Team.created_at.desc()).limit(5).all()
    # 赛事统计
    match_counts = summarize_formal_matches(active_match_query(), Match)
    return render_template('admin/dashboard.html',
                           total_users=total_users,
                           total_teams=total_teams,
                           total_captains=total_captains,
                           total_players=total_players,
                           total_members=total_members,
                           latest_teams=latest_teams,
                           tournament_status=get_tournament_status(),
                           tournament=tournament,
                           secret_key_warning=current_app.config['SECRET_KEY_WARNING'],
                           using_default_password=any(
                               current_user.check_password(password)
                               for password in known_admin_passwords(current_user.username)),
                           season_todos=get_season_todos(tournament),
                           match_counts=match_counts)


@admin_bp.route('/users')
@login_required_with_role('admin')
def users():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '', type=str)
    query = User.query
    if search:
        query = query.filter(
            db.or_(
                User.username.ilike(f'%{search}%'),
                User.nickname.ilike(f'%{search}%')))
    pagination = query.order_by(User.created_at.desc()).paginate(
        page=page, per_page=20)
    return render_template('admin/users.html',
                           users=pagination.items,
                           pagination=pagination, search=search)


@admin_bp.route('/user/<int:user_id>/promote', methods=['POST'])
@login_required_with_role('admin')
def promote_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('不能修改自己的角色', 'warning')
        return redirect(url_for('admin.users'))
    if user.is_super_admin():
        flash('不能修改超级管理员的角色', 'warning')
        return redirect(url_for('admin.users'))
    if user.role == 'captain':
        flash('该用户已是队长', 'warning')
        return redirect(url_for('admin.users'))
    user.role = 'captain'
    db.session.commit()
    log_system_action('promote_to_captain', f'将 {user.nickname}({user.username}) 提升为队长')
    db.session.commit()
    flash(f'已将 【{user.nickname}】 提升为队长', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/user/<int:user_id>/demote', methods=['POST'])
@login_required_with_role('admin')
def demote_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('不能修改自己的角色', 'warning')
        return redirect(url_for('admin.users'))
    if user.is_super_admin():
        flash('不能修改超级管理员的角色', 'warning')
        return redirect(url_for('admin.users'))
    if user.role == 'player':
        flash('该用户已是选手', 'warning')
        return redirect(url_for('admin.users'))
    if user.role == 'captain' and user.team:
        flash('队长需要先删除队伍才能降级', 'danger')
        return redirect(url_for('admin.users'))
    user.role = 'player'
    db.session.commit()
    log_system_action('demote_to_player', f'将 {user.nickname}({user.username}) 降级为选手')
    db.session.commit()
    flash(f'已将 【{user.nickname}】 降级为选手', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/user/<int:user_id>/promote-admin', methods=['POST'])
@login_required_with_role('admin')
def promote_to_admin(user_id):
    """超级管理员将选手/队长提升为管理员"""
    if not current_user.is_super_admin():
        flash('只有超级管理员可以执行此操作', 'danger')
        return redirect(url_for('admin.users'))
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('不能修改自己的角色', 'warning')
        return redirect(url_for('admin.users'))
    if user.is_super_admin():
        flash('不能修改超级管理员的角色', 'warning')
        return redirect(url_for('admin.users'))
    if user.is_admin():
        flash('该用户已是管理员', 'warning')
        return redirect(url_for('admin.users'))
    user.role = 'admin'
    db.session.commit()
    log_system_action('promote_to_admin', f'将 {user.nickname}({user.username}) 提升为管理员')
    db.session.commit()
    flash(f'已将 【{user.nickname}】 提升为管理员', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/user/<int:user_id>/demote-admin', methods=['POST'])
@login_required_with_role('admin')
def demote_from_admin(user_id):
    """超级管理员将管理员降级为选手"""
    if not current_user.is_super_admin():
        flash('只有超级管理员可以执行此操作', 'danger')
        return redirect(url_for('admin.users'))
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('不能修改自己的角色', 'warning')
        return redirect(url_for('admin.users'))
    if not user.is_admin():
        flash('该用户不是管理员', 'warning')
        return redirect(url_for('admin.users'))
    if user.is_super_admin():
        flash('不能降级超级管理员', 'danger')
        return redirect(url_for('admin.users'))
    user.role = 'player'
    db.session.commit()
    log_system_action('demote_from_admin', f'将 {user.nickname}({user.username}) 从管理员降级为选手')
    db.session.commit()
    flash(f'已将 【{user.nickname}】 降级为选手', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/user/<int:user_id>/reset-password', methods=['POST'])
@login_required_with_role('admin')
def reset_user_password(user_id):
    """管理员重置用户密码为临时密码。"""
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('不能重置自己的密码，请使用修改密码功能', 'warning')
        return redirect(url_for('admin.users'))
    if user.is_admin() and not current_user.is_super_admin():
        flash('只有超级管理员可以重置管理员账号的密码', 'danger')
        return redirect(url_for('admin.users'))
    temp_password = f'{user.username}1234'
    user.set_password(temp_password)
    user.password_changed = False
    db.session.commit()
    log_system_action('reset_password', f'重置 {user.nickname}({user.username}) 的密码')
    db.session.commit()
    flash(f'已重置 【{user.nickname}】 的密码，临时密码：{temp_password}。请通知该用户登录后修改密码。', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/user/<int:user_id>/delete', methods=['POST'])
@login_required_with_role('admin')
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('不能删除自己', 'warning')
        return redirect(url_for('admin.users'))
    if user.is_super_admin():
        flash('不能删除超级管理员', 'danger')
        return redirect(url_for('admin.users'))
    nickname = user.nickname
    # 有比赛/成绩记录的用户禁止删除（外键 NOT NULL，强删会破坏赛事数据）
    player_matches = Match.query.filter(
        (Match.player_a_id == user.id) | (Match.player_b_id == user.id)).count()
    if player_matches or ScoreLog.query.filter_by(modified_by=user.id).count():
        flash(f'无法删除【{nickname}】：该用户已产生比赛或成绩记录，删除会破坏赛事数据', 'danger')
        return redirect(url_for('admin.users'))
    # 该用户在所有赛季担任队长且已有比赛的队伍，一律禁止删除
    for team in user.teams:
        team_matches = Match.query.filter(
            (Match.team_a_id == team.id) | (Match.team_b_id == team.id)).count()
        if team_matches:
            flash(f'无法删除【{nickname}】：其队伍「{team.team_name}」已有比赛记录，请先删除相关比赛', 'danger')
            return redirect(url_for('admin.users'))
    # 清理无级联配置的关联数据（时间偏好、各赛季队伍）
    TimePreference.query.filter_by(player_id=user.id).delete()
    for team in list(user.teams):
        db.session.delete(team)
    db.session.delete(user)
    db.session.commit()
    log_system_action('delete_user', f'删除用户 {nickname}({user.username})')
    db.session.commit()
    flash(f'已删除用户 【{nickname}】', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/teams')
@login_required_with_role('admin')
def teams():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '', type=str)
    query = Team.query
    tournament = Tournament.get_active()
    if tournament:
        query = query.filter(Team.tournament_id == tournament.id)
    if search:
        query = query.filter(Team.team_name.ilike(f'%{search}%'))
    pagination = query.order_by(Team.created_at.desc()).paginate(
        page=page, per_page=20)
    return render_template('admin/teams.html',
                           teams=pagination.items,
                           pagination=pagination, search=search)


@admin_bp.route('/team/<int:team_id>')
@login_required_with_role('admin')
def team_detail(team_id):
    team = Team.query.get_or_404(team_id)
    scope = season_scope_of(team)
    if scope != 'current':
        use_season_read_context(team.tournament if scope == 'archived' else None)
    members = TeamMember.query.filter_by(team_id=team.id).all()
    score_breakdowns = {}
    for member in members:
        try:
            score_breakdowns[member.id] = get_member_score_breakdown(member.id)
        except SeasonError:
            # A legacy/orphan member remains viewable; its stored total is not
            # silently reinterpreted as a reconstructed score.
            score_breakdowns[member.id] = {
                'member_id': member.id, 'tournament_id': team.tournament_id,
                'match_score': None, 'adjustment_score': None,
                'final_score': member.final_score,
            }
    adjustments = ScoreAdjustment.query.join(
        TeamMember, ScoreAdjustment.member_id == TeamMember.id
    ).filter(TeamMember.team_id == team.id).order_by(
        ScoreAdjustment.created_at.desc(), ScoreAdjustment.id.desc()).all()
    return render_template('admin/team_detail.html', team=team, members=members,
                           season_scope=scope, read_only=scope != 'current', adjustments=adjustments,
                           score_breakdowns=score_breakdowns)


@admin_bp.route('/team/<int:team_id>/delete', methods=['POST'])
@login_required_with_role('admin')
def delete_team(team_id):
    team = Team.query.get_or_404(team_id)
    team_name = team.team_name

    # 先删除该队伍参与的所有比赛（team_a_id / team_b_id 为 NOT NULL，无法 SET NULL）
    matches_as_a = Match.query.filter_by(team_a_id=team_id).all()
    matches_as_b = Match.query.filter_by(team_b_id=team_id).all()
    for m in matches_as_a + matches_as_b:
        db.session.delete(m)
    db.session.flush()  # 先刷新删除比赛，再删队伍

    db.session.delete(team)
    db.session.commit()
    log_system_action('delete_team', f'删除队伍 {team_name}')
    db.session.commit()
    flash(f'已删除队伍 【{team_name}】', 'success')
    return redirect(url_for('admin.teams'))


@admin_bp.route('/team/<int:team_id>/update-scores', methods=['POST'])
@login_required_with_role('admin')
def update_team_scores(team_id):
    from season_safety import SeasonError, json_dump
    team = Team.query.get_or_404(team_id)
    reason = request.form.get('reason', '').strip()
    if not reason:
        raise SeasonError('人工调整必须填写原因', 400)
    for member in TeamMember.query.filter_by(team_id=team.id).all():
        raw = request.form.get(f'score_{member.id}')
        if raw not in (None, ''):
            adjustment, before, after = create_score_adjustment(
                member.id, current_user.id, reason, target_total=raw)
            if adjustment:
                log_system_action('score_adjustment', json_dump({
                    'adjustment_id': adjustment.id, 'member': member.id,
                    'tournament': team.tournament_id, 'delta': adjustment.delta,
                    'reason': reason, 'before': before, 'after': after,
                }))
    db.session.flush()
    recalculate_all_scores(team.tournament_id)
    db.session.commit()
    flash('调整分已保存，重算会保留', 'success')
    return redirect(url_for('admin.team_detail', team_id=team.id))


@admin_bp.route('/team/<int:team_id>/update-member/<int:member_id>', methods=['POST'])
@login_required_with_role('admin')
def update_member_info(team_id, member_id):
    """管理员编辑队员信息：头像、肉鸽、干员、宣言、结算分"""
    member = TeamMember.query.get_or_404(member_id)
    if member.team_id != team_id:
        flash('该成员不属于此队伍', 'danger')
        return redirect(url_for('admin.teams'))
    roguelike = request.form.get('roguelike', '').strip()
    if roguelike and roguelike != member.roguelike and roguelike not in {r.key for r in get_active_roguelikes()}:
        flash(f"请选择已启用的{get_labels()['track']}", 'danger')
        return redirect(url_for('admin.team_detail', team_id=team_id))

    # 更新头像
    if 'avatar' in request.files and request.files['avatar'].filename:
        file = request.files['avatar']
        allowed_ext = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
        ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
        if ext in allowed_ext:
            filename = f'{uuid.uuid4().hex}.{ext}'
            filepath = os.path.join(current_app.config['UPLOAD_ROOT'], 'avatars', filename)
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            try:
                file.save(filepath)
            except Exception as e:
                flash(f'头像保存失败: {str(e)}', 'danger')
                return redirect(url_for('admin.team_detail', team_id=team_id))
            member.user.avatar = filename

    # 更新肉鸽
    if roguelike:
        member.roguelike = roguelike

    # 更新分队 & 干员
    member.squad = (request.form.get('squad', '') or '').strip()
    member.operator = (request.form.get('operator', '') or '').strip()

    # 更新宣言
    declaration = request.form.get('declaration', '').strip()
    if declaration:
        member.declaration = declaration

    # A target total is converted into an independently auditable delta.
    manual_score = request.form.get('final_score', '').strip()
    if manual_score:
        from season_safety import SeasonError, json_dump
        reason = request.form.get('reason', '').strip()
        adjustment, before, after = create_score_adjustment(
            member.id, current_user.id, reason, target_total=manual_score)
        if adjustment:
            recalculate_all_scores(member.team.tournament_id)
            log_system_action('score_adjustment', json_dump({
                'adjustment_id': adjustment.id, 'member': member.id,
                'tournament': member.team.tournament_id, 'delta': adjustment.delta,
                'reason': reason, 'before': before, 'after': after,
            }))

    # 更新队伍宣言
    team_slogan = request.form.get('team_slogan', '').strip()
    if team_slogan:
        team = Team.query.get(team_id)
        if team:
            team.team_slogan = team_slogan

    db.session.commit()
    log_system_action('update_member_info', f'管理员更新队员 {member.user.nickname} 的信息')
    db.session.commit()
    flash(f'队员 {member.user.nickname} 的信息已更新', 'success')
    return redirect(url_for('admin.team_detail', team_id=team_id))


@admin_bp.route('/team/<int:team_id>/update', methods=['POST'])
@login_required_with_role('admin')
def update_team_info(team_id):
    """管理员编辑队伍信息：宣言、头像"""
    team = Team.query.get_or_404(team_id)

    team.team_slogan = (request.form.get('team_slogan', '') or '').strip()

    if 'team_avatar' in request.files and request.files['team_avatar'].filename:
        file = request.files['team_avatar']
        allowed_ext = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
        ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
        if ext in allowed_ext:
            filename = f'team_{uuid.uuid4().hex}.{ext}'
            filepath = os.path.join(current_app.config['UPLOAD_ROOT'], 'teams', filename)
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            try:
                file.save(filepath)
            except Exception as e:
                flash(f'头像保存失败: {str(e)}', 'danger')
                return redirect(url_for('admin.team_detail', team_id=team_id))
            team.team_photo = filename

    db.session.commit()
    log_system_action('update_team_info', f'管理员更新队伍 {team.team_name} 信息')
    db.session.commit()
    flash('队伍信息已更新', 'success')
    return redirect(url_for('admin.team_detail', team_id=team_id))


# ============================================================
# Admin 肉鸽管理路由
# ============================================================


@admin_bp.route('/roguelikes')
@login_required_with_role('admin')
def roguelikes():
    """肉鸽管理列表"""
    all_roguelikes = Roguelike.query.order_by(Roguelike.sort_order).all()
    return render_template('admin/roguelikes.html', roguelikes=all_roguelikes,
                           default_difficulty=get_default_difficulty())


@admin_bp.route('/roguelike/add', methods=['POST'])
@login_required_with_role('admin')
def add_roguelike():
    """新增肉鸽"""
    difficulty = parse_track_difficulty(request.form)
    track_label = get_labels()['track']
    key = request.form.get('key', '').strip()
    name = request.form.get('name', '').strip()
    max_per_team = request.form.get('max_per_team', '1')
    sort_order = request.form.get('sort_order', '0')
    if not key or not name:
        flash('标识和名称不能为空', 'danger')
        return redirect(url_for('admin.roguelikes'))
    if Roguelike.query.filter_by(key=key).first():
        flash(f'{track_label}标识 "{key}" 已存在', 'danger')
        return redirect(url_for('admin.roguelikes'))
    try:
        rl = Roguelike(
            key=key, name=name,
            max_per_team=int(max_per_team),
            sort_order=int(sort_order),
            is_active=True,
            **difficulty,
        )
        db.session.add(rl)
        db.session.commit()
        flash(f'{track_label} "{name}" 已添加', 'success')
    except ValueError:
        flash('人数限制和排序必须为数字', 'danger')
    return redirect(url_for('admin.roguelikes'))


@admin_bp.route('/roguelike/<int:rl_id>/edit', methods=['POST'])
@login_required_with_role('admin')
def edit_roguelike(rl_id):
    """编辑肉鸽"""
    rl = Roguelike.query.get_or_404(rl_id)
    difficulty = parse_track_difficulty(request.form, rl)
    rl.name = request.form.get('name', rl.name).strip()
    rl.key = request.form.get('key', rl.key).strip()
    try:
        rl.max_per_team = int(request.form.get('max_per_team', rl.max_per_team))
        rl.sort_order = int(request.form.get('sort_order', rl.sort_order))
    except ValueError:
        flash('人数限制和排序必须为数字', 'danger')
        return redirect(url_for('admin.roguelikes'))
    rl.difficulty_min = difficulty['difficulty_min']
    rl.difficulty_max = difficulty['difficulty_max']
    rl.difficulty_step = difficulty['difficulty_step']
    rl.is_active = request.form.get('is_active') == '1'
    db.session.commit()
    flash(f'{get_labels()["track"]} "{rl.name}" 已更新', 'success')
    return redirect(url_for('admin.roguelikes'))


@admin_bp.route('/roguelike/<int:rl_id>/delete', methods=['POST'])
@login_required_with_role('admin')
def delete_roguelike(rl_id):
    """删除肉鸽"""
    rl = Roguelike.query.get_or_404(rl_id)
    name = rl.name
    db.session.delete(rl)
    db.session.commit()
    flash(f'{get_labels()["track"]} "{name}" 已删除', 'success')
    return redirect(url_for('admin.roguelikes'))


# ============================================================
# Admin 轮播图管理路由
# ============================================================


@admin_bp.route('/carousel')
@login_required_with_role('admin')
def carousel():
    """轮播图管理列表"""
    tournament = Tournament.get_active()
    items = _season_carousel_items(tournament)
    return render_template('admin/carousel.html', items=items, tournament=tournament,
                           media_url=_carousel_item_media_url)


@admin_bp.route('/carousel/add', methods=['POST'])
@login_required_with_role('admin')
def add_carousel():
    """新增轮播图"""
    tournament = Tournament.get_active()
    if not tournament:
        flash('没有活跃赛事', 'warning')
        return redirect(url_for('admin.carousel'))
    media_type = request.form.get('media_type', 'image')
    file_path = request.form.get('file_path', '').strip()
    title = request.form.get('title', '').strip()
    link_url = request.form.get('link_url', '').strip()
    sort_order = request.form.get('sort_order', '0')
    if not file_path:
        flash('文件路径不能为空', 'danger')
        return redirect(url_for('admin.carousel'))
    try:
        item = CarouselItem(
            tournament_id=tournament.id,
            media_type=media_type,
            file_path=file_path,
            title=title or None,
            link_url=link_url or None,
            sort_order=int(sort_order),
            is_active=True
        )
        db.session.add(item)
        db.session.commit()
        log_system_action('add_carousel', f'添加轮播图: {title or file_path}')
        db.session.commit()
        flash('轮播图已添加', 'success')
    except ValueError:
        flash('排序值必须为数字', 'danger')
    return redirect(url_for('admin.carousel'))


@admin_bp.route('/carousel/<int:item_id>/edit', methods=['POST'])
@login_required_with_role('admin')
def edit_carousel(item_id):
    """编辑轮播图"""
    item = CarouselItem.query.get_or_404(item_id)
    item.media_type = request.form.get('media_type', item.media_type)
    item.file_path = request.form.get('file_path', item.file_path).strip()
    item.title = request.form.get('title', '').strip() or None
    item.link_url = request.form.get('link_url', '').strip() or None
    try:
        item.sort_order = int(request.form.get('sort_order', item.sort_order))
    except ValueError:
        flash('排序值必须为数字', 'danger')
        return redirect(url_for('admin.carousel'))
    item.is_active = request.form.get('is_active') == '1'
    db.session.commit()
    log_system_action('edit_carousel', f'编辑轮播图: {item.title or item.file_path}')
    db.session.commit()
    flash('轮播图已更新', 'success')
    return redirect(url_for('admin.carousel'))


@admin_bp.route('/carousel/<int:item_id>/delete', methods=['POST'])
@login_required_with_role('admin')
def delete_carousel(item_id):
    """删除轮播图"""
    item = CarouselItem.query.get_or_404(item_id)
    name = item.title or item.file_path
    db.session.delete(item)
    db.session.commit()
    log_system_action('delete_carousel', f'删除轮播图: {name}')
    db.session.commit()
    flash(f'轮播图 "{name}" 已删除', 'success')
    return redirect(url_for('admin.carousel'))


@admin_bp.route('/registration', methods=['GET', 'POST'])
@login_required_with_role('admin')
def registration_control():
    setting = Setting.get_instance()
    form = RegistrationControlForm()
    if form.validate_on_submit():
        status = form.registration_open.data == 'open'
        set_registration_open(status)
        status_text = '开放' if status else '关闭'
        flash(f'报名已{status_text}', 'success')
        return redirect(url_for('admin.registration_control'))
    elif request.method == 'GET':
        form.registration_open.data = 'open' if setting.registration_open else 'closed'
    return render_template('admin/registration_control.html',
                           form=form, registration_open=setting.registration_open,
                           opening_blocked=not has_enabled_track())


@admin_bp.route('/data-tools')
@login_required_with_role('admin')
def admin_data_tools():
    from data_tools import backup_inventory

    batches = backup_inventory() if current_user.is_super_admin() else []
    return render_template('admin/data_tools.html', batches=batches)


@admin_bp.route('/data-tools/cleanup/preview', methods=['POST'])
@login_required_with_role('admin')
def preview_cleanup():
    from data_tools import backup_inventory
    from retention import build_cleanup_plan, cleanup_options, cleanup_token

    if not current_user.is_super_admin():
        abort(403)
    options = cleanup_options(request.form)
    plan = build_cleanup_plan(options)
    token = cleanup_token(options, plan, current_user.id) if (plan.files or plan.log_ids) else None
    response = current_app.make_response(render_template('admin/data_tools.html', batches=backup_inventory(),
                                                        cleanup_plan=plan, cleanup_options=options, cleanup_token=token))
    response.cache_control.no_store = True
    return response


@admin_bp.route('/data-tools/cleanup/confirm', methods=['POST'])
@login_required_with_role('admin')
def apply_cleanup():
    from retention import confirm_cleanup

    if not current_user.is_super_admin():
        abort(403)
    if request.form.get('confirm') != '1':
        raise SeasonError('请勾选确认后再执行清理', 400)
    report = confirm_cleanup(request.form.get('cleanup_token', ''), current_user.id)
    files = sum(report['deleted_files'].values())
    logs = report['deleted_system_logs']
    failed = len(report['failed_files'])
    if failed:
        flash(f'已删除 {files} 个素材文件和 {logs} 条系统日志，{failed} 个文件未删除。请检查后重新预览。', 'warning')
    else:
        flash(f'已删除 {files} 个素材文件和 {logs} 条系统日志。', 'success')
    return redirect(url_for('admin.admin_data_tools'))


@admin_bp.route('/data-tools/score-logs')
@login_required_with_role('admin')
def export_score_logs():
    from season_safety import json_dump

    if not current_user.is_super_admin():
        abort(403)
    records = db.session.execute(db.select(ScoreLog.__table__).order_by(ScoreLog.id)).mappings()
    payload = BytesIO(json_dump([dict(record) for record in records]).encode('utf-8'))
    response = send_file(payload, mimetype='application/json', as_attachment=True, download_name='score-logs.json')
    response.cache_control.no_store = True
    return response


@admin_bp.route('/data-tools/backup', methods=['POST'])
@login_required_with_role('admin')
def create_backup_batch():
    from sqlite3 import Error as SQLiteError
    from subprocess import TimeoutExpired

    from sqlalchemy.exc import SQLAlchemyError

    from backup import create_backup, prune_backups

    if not current_user.is_super_admin():
        abort(403)
    # Authentication may have opened a read transaction on the source database.
    db.session.rollback()
    try:
        batch = create_backup(db.engine, current_app.config, 'manual-web')
    except (OSError, RuntimeError, SQLiteError, SQLAlchemyError, TimeoutExpired):
        current_app.logger.exception('后台备份失败')
        flash('备份失败，请检查数据库与素材目录。', 'danger')
        return redirect(url_for('admin.admin_data_tools'))
    log_system_action('create_backup', batch.name)
    db.session.commit()
    try:
        prune_backups(current_app.config)
    except OSError:
        current_app.logger.exception('备份已完成，旧批次清理失败')
        flash('备份已完成，旧批次清理失败。', 'warning')
    else:
        flash(f'备份已完成，批次 {batch.name}。', 'success')
    return redirect(url_for('admin.admin_data_tools'))


@admin_bp.route('/data-tools/backup/<batch>/<path:filename>')
@login_required_with_role('admin')
def download_backup(batch, filename):
    from backup import maintenance_lock
    from data_tools import INVENTORY_ERRORS, backup_download_file

    if not current_user.is_super_admin():
        abort(403)
    with maintenance_lock(current_app.config):
        try:
            path, download_name = backup_download_file(batch, filename)
            response = send_file(path, as_attachment=True, download_name=download_name)
        except INVENTORY_ERRORS:
            return '备份文件不可用。', 404
    response.cache_control.no_store = True
    return response


@admin_bp.route('/export')
@login_required_with_role('admin')
def export_excel():
    from backup import maintenance_lock
    from data_tools import build_season_workbook
    from season_safety import active_season

    with maintenance_lock(current_app.config):
        tournament = active_season()
        rankings = get_team_rankings(tournament.id)
        advancement = qualification(rankings, Setting.get_instance().advance_count or 0)
        workbook = build_season_workbook(tournament, rankings, advancement)
        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        filename = f'{get_event_name()}赛事数据_{datetime.now():%Y%m%d_%H%M%S}.xlsx'
    response = send_file(output,
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                         as_attachment=True, download_name=filename)
    response.cache_control.no_store = True
    return response


@admin_bp.route('/registration-list')
@login_required_with_role('admin')
def registration_list():
    page = request.args.get('page', 1, type=int)
    tournament = Tournament.get_active()
    query = Team.query
    if tournament:
        query = query.filter(Team.tournament_id == tournament.id)
    teams = query.order_by(Team.created_at.desc()).paginate(
        page=page, per_page=20)
    return render_template('admin/registration_list.html', teams=teams)


# ============================================================
# Admin 规则管理路由
# ============================================================


@admin_bp.route('/rules')
@login_required_with_role('admin')
def manage_rules():
    """规则管理列表"""
    all_rules = Rule.query.order_by(Rule.sort_order).all()
    all_roguelikes = get_active_roguelikes()
    form = RuleForm()
    form.roguelike_key.choices = [('', f"-- 主规则（不关联{get_labels()['track']}）--")] + [
        (track.key, track.name) for track in all_roguelikes]
    return render_template('admin/rules.html', rules=all_rules, form=form, all_roguelikes=all_roguelikes)


@admin_bp.route('/rules/add', methods=['POST'])
@login_required_with_role('admin')
def add_rule():
    """新增规则"""
    form = RuleForm()
    all_roguelikes = get_active_roguelikes()
    form.roguelike_key.choices = [('', '-- 主规则 --')] + [(r.key, r.name) for r in all_roguelikes]
    if form.validate_on_submit():
        rl_key = form.roguelike_key.data.strip() if form.roguelike_key.data else None
        rule = Rule(
            title=form.title.data.strip(),
            content=form.content.data,
            roguelike_key=rl_key or None,
            sort_order=int(form.sort_order.data or 0),
            is_published=(form.is_published.data == '1')
        )
        db.session.add(rule)
        db.session.commit()
        log_system_action('add_rule', f'新增规则: {rule.title}')
        db.session.commit()
        flash(f'规则 "{rule.title}" 已添加', 'success')
    else:
        for field, errors in form.errors.items():
            for err in errors:
                flash(f'{field}: {err}', 'danger')
    return redirect(url_for('admin.manage_rules'))


@admin_bp.route('/rules/<int:rule_id>/edit', methods=['POST'])
@login_required_with_role('admin')
def edit_rule(rule_id):
    """编辑规则"""
    rule = Rule.query.get_or_404(rule_id)
    rl_key = request.form.get('roguelike_key', '').strip()
    rule.title = request.form.get('title', rule.title).strip()
    rule.content = request.form.get('content', rule.content)
    rule.roguelike_key = rl_key or None
    try:
        rule.sort_order = int(request.form.get('sort_order', rule.sort_order))
    except ValueError:
        pass
    rule.is_published = request.form.get('is_published') == '1'
    rule.updated_at = datetime.utcnow()
    db.session.commit()
    log_system_action('edit_rule', f'编辑规则: {rule.title}')
    db.session.commit()
    flash(f'规则 "{rule.title}" 已更新', 'success')
    return redirect(url_for('admin.manage_rules'))


@admin_bp.route('/rules/<int:rule_id>/delete', methods=['POST'])
@login_required_with_role('admin')
def delete_rule(rule_id):
    """删除规则"""
    rule = Rule.query.get_or_404(rule_id)
    title = rule.title
    db.session.delete(rule)
    db.session.commit()
    log_system_action('delete_rule', f'删除规则: {title}')
    db.session.commit()
    flash(f'规则 "{title}" 已删除', 'success')
    return redirect(url_for('admin.manage_rules'))


# ============================================================
# Admin 赛事管理路由
# ============================================================


@admin_bp.route('/start-tournament', methods=['POST'])
@login_required_with_role('admin')
def start_tournament():
    """开始比赛（报名→匹配）"""
    tournament = Tournament.get_active()
    if not tournament or tournament.status != 'registration':
        flash('当前状态不允许开始比赛', 'warning')
        return redirect(url_for('admin.dashboard'))
    if not has_enabled_track():
        from season_safety import SeasonError
        raise SeasonError(no_tracks_message())

    # 验证所有队伍是否满足参赛条件
    setting = Setting.get_instance()
    teams = Team.query.filter_by(tournament_id=tournament.id).all()
    team_size = get_team_size()
    enabled_keys = {track.key for track in get_active_roguelikes()}
    track_label = get_labels()['track']
    errors = []
    for team in teams:
        members = TeamMember.query.filter_by(team_id=team.id).all()
        # 检查队伍人数（开启「允许未满员开赛」时跳过）
        if len(members) < team_size and not setting.allow_incomplete_teams:
            errors.append(f'队伍「{team.team_name}」人数不足（{len(members)}/{team_size}）')
        # 检查队员是否都选了肉鸽
        for m in members:
            if m.roguelike not in enabled_keys:
                errors.append(f'队伍「{team.team_name}」的 {m.user.nickname} 须选择已启用的{track_label}')

    if errors:
        flash('以下队伍不满足参赛条件：<br>' + '<br>'.join(errors), 'danger')
        return redirect(url_for('admin.dashboard'))

    # 开赛只变更阶段，报名由独立开关控制。
    tournament.status = 'matching'
    db.session.commit()
    log_system_action('start_tournament', '赛事进入匹配阶段')
    db.session.commit()
    flash('比赛已开始，系统进入匹配阶段', 'success')
    return redirect(url_for('admin.dashboard'))


@admin_bp.route('/run-matching', methods=['POST'])
@login_required_with_role('admin')
def run_matching():
    """执行自动匹配"""
    tournament = Tournament.get_active()
    if not tournament or tournament.status != 'matching':
        flash('当前状态不允许执行匹配', 'warning')
        return redirect(url_for('admin.dashboard'))
    if not has_enabled_track():
        from season_safety import SeasonError
        raise SeasonError(no_tracks_message())
    # 执行自动匹配
    result = _auto_match()
    if result['success']:
        tournament.status = 'running'
        db.session.commit()
        log_system_action('run_matching', f'自动匹配完成，生成 {result["count"]} 场比赛')
        db.session.commit()
        flash(f'匹配完成！共生成 {result["count"]} 场比赛', 'success')
    else:
        flash(f'匹配失败：{result["message"]}', 'danger')
    return redirect(url_for('admin.dashboard'))


@admin_bp.route('/score-config', methods=['GET', 'POST'])
@login_required_with_role('admin')
def score_config():
    """积分配置"""
    setting = Setting.get_instance()
    tournament = Tournament.get_active()
    form = ScoreConfigForm()
    if form.validate_on_submit():
        try:
            advance_count = int(form.advance_count.data)
            bonus = int(form.bonus_score.data)
        except ValueError:
            flash('晋级名额和加成倍率须为整数', 'danger')
        else:
            setting.advance_count = advance_count
            if tournament and tournament.bonus_score != bonus:
                tournament.bonus_score = bonus
                recalculate_all_scores(tournament.id)
            db.session.commit()
            flash('积分配置已更新', 'success')
        return redirect(url_for('admin.score_config'))
    elif request.method == 'GET':
        form.bonus_score.data = str(tournament.bonus_score if tournament else 500)
        form.advance_count.data = str(setting.advance_count or 0)
    return render_template('admin/score_config.html', form=form, setting=setting, tournament=tournament)


@admin_bp.route('/reset-scores', methods=['POST'])
@login_required_with_role('admin')
def reset_scores():
    """积分重置：清零所有积分并从已完成比赛重新计算"""
    recalculate_all_scores()
    db.session.commit()
    flash('积分已重置并重新计算', 'success')
    return redirect(url_for('admin.score_config'))


@admin_bp.route('/tournament-settings', methods=['GET', 'POST'])
@login_required_with_role('admin')
def tournament_settings():
    """赛事设置（名称 + 报名/比赛时间）"""
    tournament = Tournament.get_active()
    setting = Setting.get_instance()
    form = TournamentSettingsForm()
    fields = ('registration_start', 'registration_end', 'tournament_start', 'tournament_end')
    time_format = '%Y-%m-%dT%H:%M'
    original_values = {
        name: format_event_time(getattr(tournament, name), tournament.window_times_utc, time_format)
        if tournament else ''
        for name in fields
    }
    legacy_dates = bool(tournament and not tournament.window_times_utc and any(original_values.values()))
    if form.validate_on_submit():
        saved_values = {
            name: (form[name].data or '').strip() if name in request.form else original_values[name]
            for name in fields
        }
        confirm_legacy = request.form.get('confirm_legacy_windows') == '1'
        updates = None
        try:
            if legacy_dates and not confirm_legacy:
                if saved_values != original_values:
                    raise ValueError('请先确认旧日期已按页面时区核对')
            else:
                updates = {
                    name: getattr(tournament, name)
                    if tournament.window_times_utc and value == original_values[name]
                    else parse_event_time(value)
                    for name, value in saved_values.items()
                }
                for field, title in (('registration', '报名'), ('tournament', '比赛')):
                    start, end = updates[f'{field}_start'], updates[f'{field}_end']
                    if start is not None and end is not None and start >= end:
                        raise ValueError(f'{title}结束时间须晚于开始时间')
        except ValueError as error:
            flash(str(error), 'danger')
        else:
            new_name = (form.tournament_name.data or '').strip()
            if new_name:
                tournament.name = new_name
            setting.team_size = form.team_size.data
            setting.allow_incomplete_teams = bool(form.allow_incomplete_teams.data)
            if updates is not None:
                for name, value in updates.items():
                    setattr(tournament, name, value)
                tournament.window_times_utc = True
            db.session.commit()
            flash('赛事设置已更新', 'success')
            return redirect(url_for('admin.tournament_settings'))
    elif request.method == 'GET':
        form.tournament_name.data = tournament.name if tournament else current_app.config['TOURNAMENT_NAME']
        form.team_size.data = setting.team_size
        form.allow_incomplete_teams.data = bool(setting.allow_incomplete_teams)
        for name, value in original_values.items():
            form[name].data = value
    from data_tools import backup_inventory
    batches = backup_inventory()
    return render_template('admin/tournament_settings.html', form=form, tournament=tournament,
                           latest_backup=batches[0] if batches else None, legacy_dates=legacy_dates,
                           env_team_size=current_app.config['TEAM_SIZE'],
                           transition=transition_preview(tournament))


@admin_bp.route('/new-season', methods=['POST'])
@login_required_with_role('admin')
def new_season():
    from season_safety import switch_season
    result = switch_season()
    flash(f'已开启新赛季【{result.name}】，上届数据已归档保留', 'success')
    for warning in window_warnings(result).values():
        if warning:
            flash(warning, 'warning')
    if not has_enabled_track():
        flash(f'{no_tracks_message()}，本届报名开关保持关闭', 'warning')
    else:
        flash('本届报名开关保持关闭，请核对规则和赛道后手动开放', 'info')
    return redirect(url_for('admin.tournament_settings'))


@admin_bp.route('/advance')
@login_required_with_role('admin')
def advance():
    """晋级管理"""
    rankings = get_team_rankings()
    advancement = qualification(rankings, Setting.get_instance().advance_count or 0)
    records = summarize_teams(active_match_query())
    for row in rankings:
        row['record'] = records.get(row['team'].id, empty_record())
    return render_template('admin/advance.html', rankings=rankings, qualification=advancement)


@admin_bp.route('/finish-tournament', methods=['POST'])
@login_required_with_role('admin')
def finish_tournament():
    """结束赛事"""
    tournament = Tournament.get_active()
    if not tournament or tournament.status != 'running':
        flash('当前状态不允许结束赛事', 'warning')
        return redirect(url_for('admin.dashboard'))
    tournament.status = 'finished'
    db.session.commit()
    log_system_action('finish_tournament', '赛事已结束')
    db.session.commit()
    flash('赛事已结束', 'success')
    return redirect(url_for('admin.dashboard'))


@admin_bp.route('/resume-registration', methods=['POST'])
@login_required_with_role('admin')
def resume_registration():
    """恢复报名（从任何状态回到报名阶段）"""
    tournament = Tournament.get_active()
    if not tournament:
        flash('没有活跃赛事', 'warning')
        return redirect(url_for('admin.dashboard'))
    if tournament.status == 'registration':
        flash('当前已在报名阶段', 'warning')
        return redirect(url_for('admin.dashboard'))
    if not has_enabled_track():
        from season_safety import SeasonError
        raise SeasonError(no_tracks_message())
    Setting.get_instance().registration_open = True
    tournament.status = 'registration'
    db.session.commit()
    log_system_action('resume_registration', '恢复报名阶段')
    db.session.commit()
    flash('已恢复报名，赛事状态回到报名阶段', 'success')
    return redirect(url_for('admin.dashboard'))


# ============================================================
# Admin 成绩修改与审核路由
# ============================================================


@admin_bp.route('/matches')
@login_required_with_role('admin')
def matches():
    """当前赛事比赛列表"""
    page = request.args.get('page', 1, type=int)
    status_filter = request.args.get('status', '', type=str)
    query = formal_query(Match, db)
    tournament = Tournament.get_active()
    if tournament:
        query = query.filter(Match.tournament_id == tournament.id)
    if status_filter:
        query = query.filter_by(status=status_filter)
    pagination = query.order_by(Match.created_at.desc()).paginate(page=page, per_page=20)
    return render_template('admin/matches.html',
                           matches=pagination.items,
                           pagination=pagination,
                           status_filter=status_filter)


@admin_bp.route('/match/<int:match_id>/review', methods=['POST'])
@login_required_with_role('admin')
def review_match(match_id):
    """审核比赛结果"""
    from season_safety import root_match
    match = root_match(Match.query.get_or_404(match_id))
    denial = admin_result_denial('review', match)
    if denial:
        flash(denial, 'warning')
        return redirect(url_for('admin.matches'))
    action = request.form.get('action')
    reason = request.form.get('reason', '')
    if action == 'approve':
        match.review_status = 'approved'
        match.review_reason = reason
        flash('比赛结果已审核通过', 'success')
    elif action == 'reject':
        match.review_status = 'rejected'
        match.review_reason = reason
        flash('比赛结果已驳回', 'warning')
    else:
        flash('无效操作', 'danger')
        return redirect(url_for('admin.matches'))
    db.session.commit()
    return redirect(url_for('admin.matches'))


@admin_bp.route('/match/<int:match_id>/edit-result', methods=['POST'])
@login_required_with_role('admin')
def edit_match_result(match_id):
    from season_safety import (root_match, capture_match_rule, set_score, final_score, apply_outcome,
                               audit_state, json_dump, SeasonError)
    match = root_match(Match.query.get_or_404(match_id))
    denial = admin_result_denial('edit', match)
    if denial:
        raise SeasonError(denial)
    before = audit_state(match)
    was_settled = match.settlement_state == 'settled'
    reason = request.form.get('reason', '').strip()
    if was_settled and not reason:
        raise SeasonError('修改已结算成绩必须填写原因', 400)
    capture_match_rule(match)
    for side in ('a', 'b'):
        raw = request.form.get('score_' + side)
        diff = request.form.get('diff_' + side)
        if raw not in (None, '') or diff not in (None, ''):
            set_score(match, side, raw if raw not in (None, '') else getattr(match, 'player_' + side + '_score'),
                      diff if diff not in (None, '') else getattr(match, 'player_' + side + '_difficulty'),
                      getattr(match, 'score_origin_' + side) or 'admin')
        final_score(match, side)  # Both sides must have a real score and historical multiplier.
    apply_outcome(match, request.form.get('action'))
    match.settlement_state = 'settled'
    match.settled_at = datetime.utcnow()
    match.finished_at = match.finished_at or datetime.utcnow()
    _update_team_scores(match)
    after = audit_state(match)
    db.session.add(ScoreLog(match_id=match.id, modified_by=current_user.id,
                           old_result=json_dump(before), new_result=json_dump(after),
                           reason=reason or '首次确认结束并结算'))
    log_system_action('edit_result' if was_settled else 'settle_match',
                      json_dump({'match': match.id, 'season': match.tournament_id, 'before': before, 'after': after, 'reason': reason}))
    db.session.commit()
    flash('整场已确认结算' if not was_settled else '结果已修改并按本届规则重算', 'success')
    return redirect(url_for('admin.matches'))


@admin_bp.route('/match/<int:match_id>/score-logs')
@login_required_with_role('admin')
def match_score_logs(match_id):
    """查看比赛成绩修改日志"""
    match = Match.query.get_or_404(match_id)
    scope = match_scope_of(match)
    if scope != 'current':
        use_season_read_context(match.tournament if scope == 'archived' else None)
    logs = ScoreLog.query.filter_by(match_id=match_id).order_by(ScoreLog.created_at.desc()).all()
    return render_template('admin/score_logs.html', match=match, logs=logs, season_scope=scope)


# ============================================================
# Admin 手动匹配 & 日程管理
# ============================================================

def _parse_duration(note):
    """从 match_note 解析时长（分钟）"""
    stored = re.search(r'(?<!\w)duration:(\d+)', note or '')
    return int(stored[1]) if stored else None


def _parse_slot_times(base_date):
    """把 SCHEDULE_SLOT_TIMES（如 '14:00,19:00'）解析为当天的 datetime 列表"""
    slots = []
    for item in current_app.config.get('SCHEDULE_SLOT_TIMES') or []:
        try:
            hour, minute = (int(x) for x in str(item).split(':', 1))
            slots.append(base_date.replace(hour=hour, minute=minute))
        except (TypeError, ValueError):
            continue
    if not slots:
        slots = [base_date.replace(hour=14), base_date.replace(hour=19)]
    return slots


@admin_bp.route('/schedule')
@login_required_with_role('admin')
def schedule():
    """手动匹配 & 日程管理页面"""
    from sqlalchemy.sql import text

    tournament = Tournament.get_active()
    active_id = tournament.id if tournament else None
    all_teams = active_team_query().order_by(Team.team_name).all()
    all_matches = Match.query
    if tournament:
        all_matches = all_matches.filter(Match.tournament_id == active_id)
    all_matches = all_matches.order_by(Match.created_at.desc()).all()

    # 按肉鸽分组的队员
    roguelike_players = {}
    for rl in get_active_roguelikes():
        members = db.session.execute(
            text("""
                SELECT tm.id, tm.user_id, u.nickname, tm.team_id, t.team_name,
                       tm.roguelike, tm.squad, tm.operator
                FROM team_members tm
                JOIN users u ON tm.user_id = u.id
                JOIN teams t ON tm.team_id = t.id
                WHERE tm.roguelike = :key
                  AND (:tid IS NULL OR t.tournament_id = :tid)
                ORDER BY t.team_name, u.nickname
            """), {'key': rl.key, 'tid': active_id}
        ).fetchall()
        roguelike_players[rl.key] = [dict(r._mapping) for r in members]

    return render_template('admin/schedule.html',
                           tournament=tournament,
                           teams=all_teams,
                           matches=all_matches,
                           roguelike_players=roguelike_players,
                           roguelikes=get_active_roguelikes(), today=event_now().date().isoformat(),
                           schedule_open=tournament is not None and tournament.status != 'finished')


@admin_bp.route('/draw')
@login_required_with_role('admin')
def draw():
    """分组抽取器"""
    from sqlalchemy.sql import text
    tournament = Tournament.get_active()
    active_id = tournament.id if tournament else None
    roguelike_players = {}
    for rl in get_active_roguelikes():
        members = db.session.execute(
            text("""SELECT tm.id, tm.user_id, u.nickname, tm.team_id, t.team_name, tm.roguelike
                    FROM team_members tm JOIN users u ON tm.user_id=u.id JOIN teams t ON tm.team_id=t.id
                    WHERE tm.roguelike=:key AND (:tid IS NULL OR t.tournament_id = :tid)
                    ORDER BY t.team_name, u.nickname"""),
            {'key': rl.key, 'tid': active_id}
        ).fetchall()
        roguelike_players[rl.key] = [dict(r._mapping) for r in members]
    return render_template('admin/draw.html', roguelike_players=roguelike_players, roguelikes=get_active_roguelikes())


@admin_bp.route('/schedule/api/matches')
@login_required_with_role('admin')
def schedule_api_matches():
    """返回当前赛事比赛数据（JSON，供日程表前端用）—— 含子分段和表演赛"""
    matches = active_match_query().order_by(
        Match.scheduled_time.is_(None), Match.scheduled_time.asc(), Match.created_at.asc()
    ).all()
    result = []
    for m in matches:
        duration = _parse_duration(m.match_note) or 60
        protected = admin_schedule_protected(m)
        children_data = []
        for child in sorted(m.children, key=lambda c: c.scheduled_time or datetime.max):
            if child.scheduled_time:
                dur = _parse_duration(child.match_note) or current_app.config['SCHEDULE_SLOT_MINUTES']
                children_data.append({
                    'id': child.id,
                    'parent_id': m.id,
                    'start': event_local(child.scheduled_time, child.scheduled_time_utc).isoformat(),
                    'end': event_local(child.scheduled_time + timedelta(minutes=dur), child.scheduled_time_utc).isoformat(),
                    'className': m.status,
                    'editable': not protected,
                })

        result.append({
            'id': m.id,
            'player_a': m.player_a.nickname if m.player_a else '?',
            'player_b': m.player_b.nickname if m.player_b else '?',
            'team_a': m.team_a.team_name if m.team_a else '?',
            'team_b': m.team_b.team_name if m.team_b else '?',
            'roguelike': m.roguelike,
            'roguelike_name': get_roguelike_name(m.roguelike),
            'status': m.status,
            'scheduled_time': event_local(m.scheduled_time, m.scheduled_time_utc).isoformat() if m.scheduled_time else None,
            'duration_minutes': duration,
            'scheduled_end': event_local(
                m.scheduled_time + timedelta(minutes=duration),
                m.scheduled_time_utc).isoformat() if m.scheduled_time else None,
            'protected': protected,
            'is_exhibition': False,
            'children': children_data,
        })
    return jsonify({'matches': result, 'timezone': current_app.config['EVENT_TIMEZONE']})


@admin_bp.route('/schedule/manual-match', methods=['POST'])
@login_required_with_role('admin')
def manual_match():
    """手动创建比赛——自动生成下午+晚上两段，各2小时"""
    tournament = Tournament.get_active()
    player_a_id = request.form.get('player_a_id', type=int)
    player_b_id = request.form.get('player_b_id', type=int)
    roguelike = request.form.get('roguelike', '').strip()

    if not all([player_a_id, player_b_id, roguelike]):
        flash('请填写所有必填项', 'danger')
        return redirect(url_for('admin.schedule'))

    if player_a_id == player_b_id:
        flash('不能选择同一个选手', 'danger')
        return redirect(url_for('admin.schedule'))

    member_query = TeamMember.query.join(Team)
    if tournament:
        member_query = member_query.filter(Team.tournament_id == tournament.id)
    ma = member_query.filter(TeamMember.user_id == player_a_id).first()
    mb = member_query.filter(TeamMember.user_id == player_b_id).first()
    if not ma or not mb:
        flash('选手未加入队伍', 'danger')
        return redirect(url_for('admin.schedule'))
    if ma.team_id == mb.team_id:
        flash('不能选择同一队伍的两名选手', 'danger')
        return redirect(url_for('admin.schedule'))

    track_label = get_labels()['track']
    if roguelike not in {track.key for track in get_active_roguelikes()}:
        flash(f'请选择已启用的{track_label}', 'danger')
        return redirect(url_for('admin.schedule'))
    if ma.roguelike != roguelike or mb.roguelike != roguelike:
        flash(f'两名选手须已选择该{track_label}', 'danger')
        return redirect(url_for('admin.schedule'))

    date_str = request.form.get('match_date', '').strip() or event_now().date().isoformat()
    try:
        base_date = datetime.strptime(date_str, '%Y-%m-%d')
        slot_times = _parse_slot_times(base_date)
        utc_slots = [parse_event_time(slot.isoformat(timespec='minutes')) for slot in slot_times]
    except ValueError as error:
        flash(f'比赛日期或时段无效：{error}', 'danger')
        return redirect(url_for('admin.schedule'))
    duration_note = f"duration:{current_app.config['SCHEDULE_SLOT_MINUTES']}"

    # 主比赛（容器）
    match = Match(
        tournament_id=tournament.id if tournament else None,
        player_a_id=player_a_id, player_b_id=player_b_id,
        team_a_id=ma.team_id, team_b_id=mb.team_id,
        roguelike=roguelike, status='pending',
        scheduled_time=None, match_note='', segment_order=0,
    )
    db.session.add(match)
    db.session.flush()

    # 按 SCHEDULE_SLOT_TIMES 生成分段；偶数段交换对阵方向（与原「下午+晚上」逻辑一致）
    for index, slot_time in enumerate(utc_slots, start=1):
        swap = index % 2 == 0
        db.session.add(Match(
            tournament_id=match.tournament_id,
            player_a_id=player_b_id if swap else player_a_id,
            player_b_id=player_a_id if swap else player_b_id,
            team_a_id=mb.team_id if swap else ma.team_id,
            team_b_id=ma.team_id if swap else mb.team_id,
            roguelike=roguelike, status='pending',
            parent_id=match.id,
            scheduled_time=slot_time, scheduled_time_utc=True,
            match_note=duration_note,
            segment_order=index,
        ))
    db.session.commit()

    slot_text = ' & '.join(t.strftime('%H:%M') for t in slot_times)
    log_system_action('manual_match',
                      f'创建比赛 {match.id}: {ma.user.nickname} vs {mb.user.nickname}（{len(slot_times)} 个时段）')
    db.session.commit()
    flash(f'比赛已创建！{ma.user.nickname} vs {mb.user.nickname}（{slot_text}）', 'success')
    return redirect(url_for('admin.schedule'))


@admin_bp.route('/schedule/update-time', methods=['POST'])
@login_required_with_role('admin')
def schedule_update_time():
    """更新比赛时间（拖拽日程后调用）"""
    match_id = request.form.get('match_id', type=int)
    match = Match.query.get_or_404(match_id)
    times = request.form.get('scheduled_time')
    duration = request.form.get('duration_minutes', type=int)
    if times is not None:
        try:
            match.scheduled_time = parse_event_time(times)
        except ValueError as error:
            return jsonify({'error': str(error)}), 400
        match.scheduled_time_utc = True
    if duration is not None:
        if not 1 <= duration <= 1440:
            return jsonify({'error': '时长须在 1 至 1440 分钟之间'}), 400
        note = match.match_note or ''
        if re.search(r'(?<!\w)duration:\d+', note):
            note = re.sub(r'(?<!\w)duration:\d+', f'duration:{duration}', note, count=1)
        else:
            note = f'duration:{duration}' + (f'\n{note}' if note else '')
        match.match_note = note

    db.session.commit()
    return jsonify({'success': True, 'match_id': match.id})


@admin_bp.route('/schedule/split-match', methods=['POST'])
@login_required_with_role('admin')
def schedule_split_match():
    """剪断比赛：把一场比赛切分成多个独立时段，每段不同主次选手"""
    match_id = request.form.get('match_id', type=int)
    parts = request.form.get('parts', default=2, type=int)
    if parts not in (2, 3, 4):
        return jsonify({'error': '只支持拆成 2 至 4 段'}), 400

    match = Match.query.get_or_404(match_id)
    if not match.scheduled_time:
        return jsonify({'error': '比赛还未安排时间'}), 400

    # 先删除所有旧的子分段（重新来过）
    for old_child in match.children:
        db.session.delete(old_child)
    db.session.flush()

    total_dur = _parse_duration(match.match_note) or 60
    seg_dur = total_dur // parts
    start = match.scheduled_time
    pa_name = match.player_a.nickname if match.player_a else 'A'
    pb_name = match.player_b.nickname if match.player_b else 'B'

    for i in range(parts):
        seg_start = start + timedelta(minutes=i * seg_dur)
        if i == 0:
            # 第一段保持原样（选手A主，选手B副）
            match.scheduled_time = seg_start
            match.match_note = f'duration:{seg_dur}|host:{pa_name}'
            match.segment_order = 0
        else:
            # 后续分段：交换主次选手
            if i % 2 == 1:
                pa, pb = match.player_a_id, match.player_b_id
                ta, tb = match.team_a_id, match.team_b_id
                host_name = pa_name
            else:
                pa, pb = match.player_b_id, match.player_a_id
                ta, tb = match.team_b_id, match.team_a_id
                host_name = pb_name
            child = Match(
                tournament_id=match.tournament_id,
                player_a_id=pa, player_b_id=pb,
                team_a_id=ta, team_b_id=tb,
                roguelike=match.roguelike,
                status='pending',
                match_type='match',
                parent_id=match.id,
                segment_order=i,
                scheduled_time=seg_start, scheduled_time_utc=match.scheduled_time_utc,
                match_note=f'duration:{seg_dur}|host:{host_name}',
            )
            db.session.add(child)

    db.session.commit()
    return jsonify({'success': True, 'parts': parts, 'seg_dur': seg_dur})


@admin_bp.route('/schedule/insert-exhibition', methods=['POST'])
@login_required_with_role('admin')
def schedule_insert_exhibition():
    """在两段比赛之间插入表演赛"""
    parent_id = request.form.get('parent_id', type=int)
    player_name = request.form.get('player_name', '').strip()
    scheduled_time_str = request.form.get('scheduled_time', '').strip()
    duration_minutes = request.form.get('duration_minutes', type=int) or 30
    roguelike = request.form.get('roguelike', '').strip()

    parent = Match.query.get_or_404(parent_id)
    if not parent.scheduled_time:
        return jsonify({'error': '父比赛未安排时间'}), 400

    scheduled_time = None
    if scheduled_time_str:
        try:
            scheduled_time = parse_event_time(scheduled_time_str)
        except ValueError:
            return jsonify({'error': '时间格式无效'}), 400

    match = Match(
        tournament_id=parent.tournament_id,
        player_a_id=parent.player_a_id,
        player_b_id=parent.player_b_id,
        team_a_id=None,
        team_b_id=None,
        roguelike=roguelike or parent.roguelike,
        status='pending',
        match_type='exhibition',
        parent_id=parent.id,
        segment_order=len(parent.children) + 1,
        scheduled_time=scheduled_time, scheduled_time_utc=True,
        match_note=f'exhibition:{player_name}|duration:{duration_minutes}',
    )
    db.session.add(match)
    db.session.commit()
    return jsonify({'success': True, 'match_id': match.id})


@admin_bp.route('/schedule/quick-exhibition', methods=['POST'])
@login_required_with_role('admin')
def schedule_quick_exhibition():
    """点击日历空白区域快速创建表演赛"""
    roguelike = request.form.get('roguelike', '').strip()
    scheduled_time_str = request.form.get('scheduled_time', '').strip()
    duration_minutes = request.form.get('duration_minutes', type=int) or 30

    if not roguelike:
        return jsonify({'error': f"请先在左侧选择{get_labels()['track']}"}), 400

    scheduled_time = None
    if scheduled_time_str:
        try:
            scheduled_time = parse_event_time(scheduled_time_str)
        except ValueError:
            pass

    match = Match(
        tournament_id=Tournament.get_active().id if Tournament.get_active() else None,
        player_a_id=User.query.filter_by(role='admin').first().id if User.query.filter_by(role='admin').first() else 1,
        player_b_id=User.query.filter_by(role='admin').first().id if User.query.filter_by(role='admin').first() else 1,
        team_a_id=None, team_b_id=None,
        roguelike=roguelike,
        status='pending',
        scheduled_time=scheduled_time, scheduled_time_utc=True,
        match_note=f'duration:{duration_minutes}',
    )
    db.session.add(match)
    db.session.commit()
    rl_name = get_roguelike_name(roguelike)
    return jsonify({'success': True, 'match_id': match.id, 'title': f'🎭 表演赛 - {rl_name}'})


@admin_bp.route('/schedule/delete-match', methods=['POST'])
@login_required_with_role('admin')
def schedule_delete_match():
    """删除比赛"""
    match_id = request.form.get('match_id', type=int)
    match = Match.query.get_or_404(match_id)
    info = f'{match.player_a.nickname if match.player_a else "?"} vs {match.player_b.nickname if match.player_b else "?"}'
    db.session.delete(match)
    db.session.commit()
    log_system_action('delete_match', f'删除比赛 {match.id}: {info}')
    db.session.commit()
    flash(f'已删除比赛: {info}', 'info')
    return redirect(url_for('admin.schedule'))


@admin_bp.route('/schedule/confirm-match', methods=['POST'])
@login_required_with_role('admin')
def schedule_confirm_match():
    """管理员一键确认比赛（跳过 pending→scheduled→confirmed 流程）"""
    match_id = request.form.get('match_id', type=int)
    from season_safety import root_match
    match = root_match(Match.query.get_or_404(match_id))
    match.status = 'confirmed'
    match.confirmed_a = True
    match.confirmed_b = True
    # 同时确认所有子分段
    for child in match.children:
        child.status = 'confirmed'
        child.confirmed_a = True
        child.confirmed_b = True
    db.session.commit()
    log_system_action('confirm_match', f'管理员确认比赛 {match.id} 及其子分段')
    db.session.commit()
    flash(f'比赛已确认！选手现在可以提交成绩', 'success')
    return redirect(url_for('admin.schedule'))


# ============================================================
# 自动匹配逻辑（实现见 matching.py）
# ============================================================


# ============================================================
# Tournament 路由
# ============================================================


@tournament_bp.route('/')
def hall():
    """赛事大厅"""
    tournament = Tournament.get_active()
    matches = active_match_query()
    rankings = get_team_rankings()
    quota = (Setting.get_instance().advance_count or 0) if tournament else 0
    advancement = qualification(rankings, quota)
    return render_template('tournament/hall.html',
                           tournament=tournament,
                           tournament_status=get_tournament_status(),
                           match_counts=summarize_formal_matches(matches, Match),
                           rankings=rankings, qualification=advancement,
                           recent_matches=matches.order_by(Match.created_at.desc()).limit(30).all())


@tournament_bp.route('/rankings')
def rankings():
    """排行榜"""
    rankings = get_team_rankings()
    return render_template('tournament/rankings.html', rankings=rankings)


@tournament_bp.route('/results')
def results():
    """赛事结果页（赛事结束后展示冠亚季军）"""
    tournament = Tournament.get_active()
    rankings = get_team_rankings()
    podiums = {rank: [row for row in rankings if row['rank'] == rank] for rank in (1, 2, 3)}
    return render_template('tournament/results.html',
                           tournament=tournament,
                           tournament_status=get_tournament_status(),
                           champions=podiums[1], runner_ups=podiums[2], third_places=podiums[3],
                           champion=podiums[1][0] if podiums[1] else None,
                           runner_up=podiums[2][0] if podiums[2] else None,
                           third_place=podiums[3][0] if podiums[3] else None,
                           rankings=rankings)


@tournament_bp.route('/my-matches')
@login_required_with_role('player', 'captain')
def my_matches():
    scope = requested_record_scope()
    user_id = current_user.id
    query, season_id = player_record_query(user_id, scope=scope)
    pager = query.order_by(Match.created_at.desc(), Match.id.desc()).paginate(
        page=request.args.get('page', 1, type=int), per_page=20)
    season = Tournament.get_active()
    return render_template('tournament/my_matches.html', matches=pager.items, pagination=pager,
                           scope=scope, season_id=season_id,
                           match_actions={
                               match.id: not player_match_denial('submit_result', match, user_id, season)
                               and match_scope_of(match) == 'current'
                               for match in pager.items
                           })


@tournament_bp.route('/match/<int:match_id>')
@login_required_with_role('player', 'captain', 'admin')
def match_detail(match_id):
    """比赛详情"""
    from season_safety import root_match
    match = root_match(Match.query.get_or_404(match_id))
    is_participant = (current_user.id == match.player_a_id or
                      current_user.id == match.player_b_id)
    if not current_user.is_admin() and not is_participant:
        flash('您没有权限查看此比赛', 'danger')
        return redirect(url_for('tournament.my_matches'))
    scope = match_scope_of(match)
    if scope != 'current':
        use_season_read_context(match.tournament if scope == 'archived' else None)
    season = match.tournament if scope == 'current' else None
    allowed = {action: player_match_denial(action, match, current_user.id, season) is None
               for action in ('schedule_match', 'confirm_schedule', 'submit_result', 'forfeit_match', 'send_match_note')}
    return render_template('tournament/match_detail.html', match=match,
                           is_participant=is_participant, allowed=allowed, season_scope=scope)


def require_player_match_action(match_id, action):
    from season_safety import root_match, verify_match_ownership

    match = root_match(Match.query.get_or_404(match_id))
    denial = player_match_denial(action, match, current_user.id, Tournament.get_active())
    if denial:
        raise SeasonError(*denial)
    if request.method == 'GET':
        verify_match_ownership(match)
    return match


@tournament_bp.route('/match/<int:match_id>/schedule', methods=['GET', 'POST'])
@login_required_with_role('player', 'captain')
def schedule_match(match_id):
    """确认比赛时间（线下商讨后在此确定）"""
    match = require_player_match_action(match_id, 'schedule_match')
    is_a = current_user.id == match.player_a_id
    already_confirmed = match.confirmed_a if is_a else match.confirmed_b

    form = MatchScheduleForm()
    if form.validate_on_submit():
        try:
            scheduled_time = parse_event_time(form.scheduled_time.data.replace(' ', 'T'))
            if match.scheduled_time != scheduled_time or not match.scheduled_time_utc:
                match.confirmed_a = False
                match.confirmed_b = False
            match.scheduled_time = scheduled_time
            match.scheduled_time_utc = True
            # 设置时间的人自动视为已确认
            if is_a:
                match.confirmed_a = True
            else:
                match.confirmed_b = True
            # 检查是否双方都已确认
            if match.confirmed_a and match.confirmed_b:
                match.status = 'confirmed'
                flash('比赛时间已确认，双方达成一致！', 'success')
            else:
                match.status = 'scheduled'
                flash('比赛时间已记录，等待对手确认', 'success')
            db.session.commit()
        except ValueError:
            flash('时间格式错误，请使用 YYYY-MM-DD HH:MM 格式', 'danger')
        return redirect(url_for('tournament.match_detail', match_id=match_id))
    return render_template('tournament/schedule_match.html',
                           form=form, match=match, already_confirmed=already_confirmed)


@tournament_bp.route('/match/<int:match_id>/confirm-schedule', methods=['POST'])
@login_required_with_role('player', 'captain')
def confirm_schedule(match_id):
    """确认对手设定的比赛时间"""
    match = require_player_match_action(match_id, 'confirm_schedule')
    is_a = current_user.id == match.player_a_id
    if is_a:
        match.confirmed_a = True
    else:
        match.confirmed_b = True
    if match.confirmed_a and match.confirmed_b:
        match.status = 'confirmed'
        flash('比赛时间已确认，双方达成一致！', 'success')
    else:
        flash('已确认比赛时间', 'success')
    db.session.commit()
    return redirect(url_for('tournament.match_detail', match_id=match_id))


@tournament_bp.route('/match/<int:match_id>/forfeit', methods=['POST'])
@login_required_with_role('player', 'captain')
def forfeit_match(match_id):
    from season_safety import capture_match_rule, set_score, json_dump
    match = require_player_match_action(match_id, 'forfeit_match')
    reason = request.form.get('reason', '').strip() or '选手主动弃权'
    capture_match_rule(match)
    side = 'a' if current_user.id == match.player_a_id else 'b'
    if getattr(match, 'player_' + side + '_score') is None:
        rule = json.loads(match.scoring_rule)
        set_score(match, side, '0', rule['minimum'], 'forfeit_zero')
    match.forfeit_side = side
    match.forfeit_reason = reason
    from season_safety import apply_outcome
    apply_outcome(match, None)
    # Winner is known immediately, but scores are booked only after an explicit admin settlement.
    match.settlement_state = 'pending'
    log_system_action('forfeit', json_dump({'match': match.id, 'season': match.tournament_id,
                                           'side': side, 'reason': reason, 'settlement': 'pending'}))
    db.session.commit()
    flash('已记录弃权；对手可补交成绩，等待管理员确认结算', 'info')
    return redirect(url_for('tournament.match_detail', match_id=match.id))


@tournament_bp.route('/match/<int:match_id>/note', methods=['POST'])
@login_required_with_role('player', 'captain')
def send_match_note(match_id):
    """发送比赛留言"""
    match = require_player_match_action(match_id, 'send_match_note')
    form = MatchNoteForm()
    if form.validate_on_submit():
        note = form.match_note.data
        if match.match_note:
            match.match_note += f'\n[{event_now().strftime("%m-%d %H:%M")}] {current_user.nickname}: {note}'
        else:
            match.match_note = f'[{event_now().strftime("%m-%d %H:%M")}] {current_user.nickname}: {note}'
        db.session.commit()
        flash('留言已发送', 'success')
    return redirect(url_for('tournament.match_detail', match_id=match_id))


@tournament_bp.route('/match/<int:match_id>/submit-result', methods=['GET', 'POST'])
@login_required_with_role('player', 'captain')
def submit_result(match_id):
    from season_safety import capture_match_rule, get_match_scoring_rule, set_score, SeasonError
    match = require_player_match_action(match_id, 'submit_result')
    side = 'a' if current_user.id == match.player_a_id else 'b'
    if request.method == 'POST':
        # Validate before touching uploads; duplicates and archived IDs create no files.
        capture_match_rule(match)
        set_score(match, side, request.form.get('score'), request.form.get('difficulty'))
        files = request.files.getlist('screenshots')
        staged = []
        try:
            for file in files:
                if not file or not file.filename:
                    continue
                ext = file.filename.rsplit('.', 1)[-1].lower()
                if ext not in {'png', 'jpg', 'jpeg', 'webp'}:
                    raise SeasonError('截图格式无效', 400)
                filename = f'screenshot_{match.id}_{current_user.id}_{uuid.uuid4().hex}.{ext}'
                directory = os.path.join(current_app.config['UPLOAD_ROOT'], 'screenshots')
                os.makedirs(directory, exist_ok=True)
                path = os.path.join(directory, filename)
                staged.append(path)
                file.save(path)
                match.match_note = (match.match_note or '') + f'\n[截图] {current_user.nickname}: /static/uploads/screenshots/{filename}'
            setattr(match, 'player_' + side + '_result', 'submitted')
            if not match.forfeit_side and match.player_a_score is not None and match.player_b_score is not None:
                match.status = 'submitted'
            current_user.status = 6
            db.session.commit()
        except Exception:
            db.session.rollback()
            for path in staged:
                if os.path.isfile(path):
                    os.remove(path)
            raise
        flash('整场成绩已保存，等待确认结束并结算', 'success')
        return redirect(url_for('tournament.match_detail', match_id=match.id))
    return render_template('tournament/submit_result.html', match=match,
                           difficulty_rule=get_match_scoring_rule(match))


# ============================================================
# User Loader
# ============================================================


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ============================================================
# 应用工厂
# ============================================================


def known_admin_passwords(username):
    """Recognize the legacy bootstrap default and the existing reset convention."""
    return ('admin123', f'{username}1234')


def configure_credentials(app, *, production):
    for name in ('SECRET_KEY', 'ADMIN_PASSWORD'):
        if production and not app.config[name]:
            raise RuntimeError(f'生产环境必须显式设置 {name}')

    warning = None
    if not app.config['SECRET_KEY']:
        app.config['SECRET_KEY'] = secrets.token_urlsafe(32)
        warning = '未设置 SECRET_KEY，当前进程使用临时会话密钥。重启后需要重新登录。'
    elif app.config['SECRET_KEY'] in (
            'dev-secret-key-change-in-production', 'change-me-in-production',
            '请显式设置随机长字符串', '请改成随机长字符串'):
        warning = '正在使用已知的默认会话密钥，请设置独立的随机 SECRET_KEY。'
    app.config['SECRET_KEY_WARNING'] = warning
    if warning:
        app.logger.warning(warning)

    if not app.config['ADMIN_PASSWORD']:
        app.logger.warning('未设置 ADMIN_PASSWORD。首次创建管理员时需要该配置，既有账号使用数据库中的密码。')
    for prefix in ('ADMIN', 'SUPER_ADMIN'):
        username = app.config[f'{prefix}_USERNAME']
        password = app.config[f'{prefix}_PASSWORD']
        if username and password in (*known_admin_passwords(username), '请显式设置强口令', '请改成强密码'):
            app.logger.warning('%s 使用已知的默认口令或配置示例，请为新账号配置独立口令。', f'{prefix}_PASSWORD')


def create_app(config_name=None, overrides=None):
    """应用工厂"""
    if config_name is None:
        config_name = os.environ.get('FLASK_ENV', 'development')

    # 显式指定模板文件夹为 app/templates（保留原有模板结构）
    app = Flask(__name__,
                   template_folder='app/templates',
                   static_folder='app/static')
    app.config.from_object(config[config_name])
    app.config.update(SECRET_KEY=os.environ.get('SECRET_KEY'),
                      ADMIN_PASSWORD=os.environ.get('ADMIN_PASSWORD', ''))
    if overrides:
        app.config.update(overrides)
    configure_credentials(app, production=(config_name == 'production'))
    if 'SQLALCHEMY_DATABASE_URI' not in app.config:
        app.config['SQLALCHEMY_DATABASE_URI'] = database_uri_from_environment()
    with app.app_context():
        event_zone()  # Fail at startup on a misspelled timezone.
    from sqlalchemy.engine import make_url
    uri = make_url(app.config['SQLALCHEMY_DATABASE_URI'])
    backend = uri.get_backend_name()
    if config_name == 'testing':
        # Explicitly supplied temporary paths belong to this factory, never module globals.
        app.config['WTF_CSRF_ENABLED'] = False
    options = dict(app.config.get('SQLALCHEMY_ENGINE_OPTIONS') or {})
    options.setdefault('hide_parameters', True)
    if backend == 'sqlite':
        options['connect_args'] = dict(options.get('connect_args', {}), check_same_thread=False, timeout=30)
    elif backend == 'mysql':
        options.get('connect_args', {}).pop('check_same_thread', None)
        options.setdefault('isolation_level', 'READ COMMITTED')
    else:
        raise RuntimeError('数据库仅支持 SQLite 或 MySQL')
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = options

    db.init_app(app)
    if backend == 'sqlite':
        from sqlalchemy import event
        with app.app_context():
            @event.listens_for(db.engine, 'connect')
            def sqlite_foreign_keys(connection, record):
                connection.execute('PRAGMA foreign_keys=ON')
    login_manager.init_app(app)
    csrf.init_app(app)
    migrate.init_app(app, db)

    # 注册蓝图
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(player_bp)
    app.register_blueprint(captain_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(tournament_bp)

    login_manager.login_view = 'auth.login'
    login_manager.login_message = '请先登录'
    login_manager.login_message_category = 'warning'

    # 注册模板全局函数
    app.jinja_env.globals['get_roguelike_name'] = get_roguelike_name
    app.jinja_env.globals['get_match_track_name'] = get_match_track_name
    app.jinja_env.globals['get_tournament_status'] = get_tournament_status
    app.jinja_env.filters['event_time'] = format_event_time
    app.jinja_env.globals['admin_result_denial'] = admin_result_denial
    app.jinja_env.globals['has_outcome'] = has_outcome
    app.jinja_env.globals['is_settled'] = is_settled
    app.jinja_env.globals['outcome_for_player'] = outcome_for_player
    app.jinja_env.globals['admin_schedule_protected'] = admin_schedule_protected
    app.jinja_env.globals['get_active_roguelikes'] = get_active_roguelikes
    app.jinja_env.globals['_parse_duration'] = _parse_duration
    app.jinja_env.globals['timedelta'] = timedelta

    # 添加 nl2br 过滤器：将纯文本换行符转换为 <br> 标签
    @app.template_filter('nl2br')
    def nl2br_filter(text):
        if not text:
            return ''
        from markupsafe import escape, Markup
        return Markup(escape(text).replace('\n', '<br>'))

    # 上下文处理器：注入新手引导状态
    @app.context_processor
    def inject_onboarding():
        if current_user.is_authenticated:
            return {
                'show_onboarding': not current_user.has_completed_onboarding and not current_user.is_admin(),
                'has_completed_onboarding': current_user.has_completed_onboarding,
                'tutorial_step': current_user.tutorial_step,
            }
        return {
            'show_onboarding': False,
            'has_completed_onboarding': False,
        }

    # 上下文处理器：注入赛事名称 + 领域术语 + 品牌主题（模板中通过 {{ event_name }} / {{ labels.track }} / {{ theme.brand_color }} 使用）
    @app.context_processor
    def inject_event_info():
        labels = get_labels()
        settings_id = db.select(db.func.min(Setting.id)).scalar_subquery()
        track_exists = db.select(Roguelike.id).where(Roguelike.is_active.is_(True)).exists()
        # Seed a row so the layout can render with no active season or saved settings.
        rows = db.session.execute(
            db.select(Tournament, Setting, track_exists)
            .select_from(db.select(db.literal(1)).subquery())
            .outerjoin(Tournament, Tournament.is_active.is_(True))
            .outerjoin(Setting, Setting.id == settings_id)
            .limit(2)
        ).all()
        if len(rows) > 1:
            raise SeasonError('检测到多个活跃赛季，请先按核对报告修复', 409)
        tournament, setting, tracks_enabled = rows[0]
        context = getattr(g, 'historical_context', None)
        name = context['name'] if context else (
            (tournament.name if tournament else None) or _resolve_config(
                'TOURNAMENT_NAME', 'TOURNAMENT_NAME', '赛事管理系统',
                lambda: setting.tournament_name if setting else None))
        team_size = context['team_size'] if context else int(_resolve_config(
            'TEAM_SIZE', 'TEAM_SIZE', 4, lambda: (setting.team_size or None) if setting else None))
        season_info = {'name': tournament.name, 'number': tournament.season_number} if tournament else {
            'name': '', 'number': None}
        if context:
            season_info = {'name': context['name'], 'number': context.get('season_number', season_info['number'])}
        return {
            'event_name': name,
            'registration_problem': registration_denial(
                tournament, tracks_enabled=tracks_enabled,
                registration_open=bool(setting.registration_open) if setting else True),
            'tracks_enabled': tracks_enabled,
            'active_season_id': tournament.id if tournament else None,
            'event_year': event_now().year,
            'labels': labels,
            'team_size': team_size,
            'theme': {
                'brand_color': current_app.config.get('BRAND_PRIMARY_COLOR', '#0d6efd'),
                'track_label': labels['track'],
            },
            'current_season': season_info,
        }

    # 404 处理：非指定页面全部跳回主页
    @app.errorhandler(404)
    def page_not_found(e):
        return redirect(url_for('main.index'))

    from season_safety import install_safety
    from management import register_commands
    install_safety(app)
    register_commands(app)

    return app


# ============================================================
# Importing this module creates no application, directories, tables or data.
# Production entry: gunicorn wsgi:app. Preparation: flask --app 'app:create_app()' prepare.
# ============================================================
if __name__ == '__main__':
    runtime_app = create_app()
    from migrations import assert_schema_current
    with runtime_app.app_context():
        assert_schema_current(db.engine)
    runtime_app.run(host='0.0.0.0', port=runtime_app.config['PORT'], load_dotenv=False)

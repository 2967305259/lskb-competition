"""
赛事报名管理系统 - 单文件版
所有后端代码整合于此文件，适用于 Docker 打包部署
赛事名称、端口、队伍人数、初始账号等均可用环境变量覆盖（见 Config 类）
"""

import os
import sys
import uuid
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO

from flask import (Flask, Blueprint, render_template, redirect, url_for,
                   flash, request, abort, send_file, jsonify, current_app)
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
from openpyxl import Workbook

# 业务规则模块（积分/倍率、自动匹配）；模块内部延迟导入本文件的模型，无循环依赖
from scoring import (calculate_multiplier, get_team_points, get_team_rankings,
                     recalculate_all_scores, _update_team_scores)
from matching import _auto_match

# ============================================================
# 配置
# ============================================================

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_DIR = os.path.join(BASE_DIR, '.db')
DB_FILE = os.path.join(DB_DIR, 'lskb.db')
UPLOAD_DIR = os.path.join(BASE_DIR, 'app', 'static', 'uploads', 'avatars')
TEAM_UPLOAD_DIR = os.path.join(BASE_DIR, 'app', 'static', 'uploads', 'teams')

# 确保 .db 目录存在
os.makedirs(DB_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(TEAM_UPLOAD_DIR, exist_ok=True)


class Config:
    """基础配置"""
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or (
        'sqlite:///' + DB_FILE.replace('\\', '/'))
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'connect_args': {'check_same_thread': False},
        'echo': False
    }
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
    # 初始账号（仅首次启动无对应用户时创建）
    ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')
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


def check_registration_open():
    """检查报名是否开放"""
    setting = Setting.get_instance()
    return setting.registration_open


def set_registration_open(status):
    """设置报名状态"""
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
    """赛事名称：优先数据库 Setting（后台可改），回退环境变量配置"""
    def _from_setting():
        s = Setting.query.first()
        return (s.tournament_name or None) if s else None

    return _resolve_config('TOURNAMENT_NAME', 'TOURNAMENT_NAME', '赛事管理系统', _from_setting)


def get_team_size():
    """队伍满员人数（含队长）：优先数据库 Setting.team_size，回退环境变量"""
    def _from_setting():
        s = Setting.query.first()
        return (s.team_size or None) if s else None

    return int(_resolve_config('TEAM_SIZE', 'TEAM_SIZE', 4, _from_setting))


def get_team_positions():
    """队内位置列表（环境变量为逗号分隔字符串）"""
    value = _resolve_config('TEAM_POSITIONS', 'TEAM_POSITIONS',
                            ['cold', 'normal1', 'normal2', 'pit'])
    if isinstance(value, str):
        return [p.strip() for p in value.split(',') if p.strip()]
    return value


def get_labels():
    """领域术语文案（track=赛道/玩法，field1/field2=队员两个自定义字段）"""
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
    img_dir = os.path.join(BASE_DIR, 'image')
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


def get_roguelike_name(key):
    """从数据库获取肉鸽名称（回退到硬编码）"""
    rl = Roguelike.query.filter_by(key=key).first()
    if rl:
        return rl.name
    # 回退
    fallback = {
        'water': '水月与深蓝之树',
        'sami': '探索者的银凇止境',
        'sarkaz': '萨卡兹的无终奇语',
        'garden': '岁的界园志异',
    }
    return fallback.get(key, key)


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


def set_tournament_status(status):
    """设置赛事状态"""
    tournament = Tournament.get_active()
    if tournament:
        tournament.status = status
        db.session.commit()


def is_tournament_running():
    """赛事是否在进行中（匹配或比赛阶段）"""
    status = get_tournament_status()
    return status in ('matching', 'running')


def is_tournament_finished():
    """赛事是否已结束"""
    return get_tournament_status() == 'finished'


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
    db.session.commit()


def get_advanced_teams():
    """获取晋级队伍"""
    setting = Setting.get_instance()
    if setting.advance_count <= 0:
        return []
    rankings = get_team_rankings()
    return rankings[:setting.advance_count]


# ============================================================
# 赛季作用域（列表/统计查询统一限定在当前赛事）
# ============================================================


def active_team_query():
    """当前赛事的队伍查询；无活跃赛事时返回全部（兼容空库）"""
    tournament = Tournament.get_active()
    return Team.query.filter(Team.tournament_id == tournament.id) if tournament else Team.query


def active_match_query(*criteria):
    """当前赛事的父比赛查询；无活跃赛事时返回全部（兼容空库）"""
    query = Match.query.filter(Match.parent_id.is_(None))
    tournament = Tournament.get_active()
    if tournament:
        query = query.filter(Match.tournament_id == tournament.id)
    for criterion in criteria:
        query = query.filter(criterion)
    return query


def in_current_season(team):
    """队伍是否属于当前赛事；tournament_id 为空的历史数据视为兼容放行"""
    if team is None:
        return False
    tournament = Tournament.get_active()
    if tournament is None:
        return True
    return team.tournament_id is None or team.tournament_id == tournament.id


# ============================================================
# 模型 (Models)
# ============================================================


class Tournament(db.Model):
    """赛事赛季模型"""
    __tablename__ = 'tournaments'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, default='')
    season_number = db.Column(db.Integer, nullable=False, default=1)
    is_active = db.Column(db.Boolean, default=True)
    status = db.Column(db.String(20), default='registration')  # draft, registration, matching, running, finished
    registration_start = db.Column(db.DateTime, nullable=True)
    registration_end = db.Column(db.DateTime, nullable=True)
    tournament_start = db.Column(db.DateTime, nullable=True)
    tournament_end = db.Column(db.DateTime, nullable=True)
    bonus_score = db.Column(db.Integer, default=500)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    teams = db.relationship('Team', backref='tournament', lazy='dynamic')
    matches = db.relationship('Match', backref='tournament', lazy='dynamic')
    carousel_items = db.relationship('CarouselItem', backref='tournament', lazy='dynamic', cascade='all, delete-orphan')

    @classmethod
    def get_active(cls):
        return cls.query.filter_by(is_active=True).first()

    def __repr__(self):
        return f'<Tournament {self.name}>'


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
    tournament_status = db.Column(db.String(20), default='registration')
    # 晋级数量
    advance_count = db.Column(db.Integer, default=0)
    # 新增可配置参数
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
            db.session.commit()
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
    match_note = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)

    player_a = db.relationship('User', foreign_keys=[player_a_id], backref='matches_as_a')
    player_b = db.relationship('User', foreign_keys=[player_b_id], backref='matches_as_b')
    team_a = db.relationship('Team', foreign_keys=[team_a_id], backref='matches_as_a')
    team_b = db.relationship('Team', foreign_keys=[team_b_id], backref='matches_as_b')
    winner = db.relationship('User', foreign_keys=[winner_id], backref='matches_won')
    children = db.relationship('Match', backref=db.backref('parent', remote_side=[id]), cascade='all, delete-orphan')

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
    old_result = db.Column(db.String(20), nullable=True)
    new_result = db.Column(db.String(20), nullable=True)
    reason = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    match = db.relationship('Match', backref='score_logs')
    modifier = db.relationship('User', backref='score_logs')

    def __repr__(self):
        return f'<ScoreLog match={self.match_id}>'


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
    detail = db.Column(db.Text, default='')
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
    roguelike = SelectField('选择肉鸽', choices=[], validators=[DataRequired()])
    squad = StringField('所选分队', validators=[
        Optional(),
        Length(max=50, message='分队名称最多50个字符'),
    ])
    operator = StringField('开局干员', validators=[
        Optional(),
        Length(max=50, message='干员名称最多50个字符'),
    ])
    submit = SubmitField('确认选择')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.roguelike.choices = get_roguelike_choices()


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


class MatchResultForm(FlaskForm):
    """比赛结果提交表单（难度动态生成）"""
    difficulty = SelectField('难度', choices=[], validators=[DataRequired()])
    score = StringField('比赛分数', validators=[
        DataRequired(message='请输入比赛分数'),
    ])
    result = SelectField('我的比赛结果', choices=[
        ('win', '我获胜'),
        ('lose', '我失败'),
        ('draw', '平局'),
    ], validators=[DataRequired()])
    submit = SubmitField('提交结果')

    def set_difficulty_choices(self, roguelike_key):
        """根据肉鸽动态设置难度选项"""
        rl = Roguelike.query.filter_by(key=roguelike_key).first()
        if rl:
            choices = []
            for d in range(rl.difficulty_min, rl.difficulty_max + 1):
                mult = rl.get_multiplier(d)
                choices.append((str(d), f'{d} 难 (×{mult:.1f})'))
            self.difficulty.choices = choices
            self.difficulty.default = str(rl.difficulty_min)
        else:
            # 回退到默认 12-15
            self.difficulty.choices = [
                ('12', '12 难 (×1.0)'), ('13', '13 难 (×1.1)'),
                ('14', '14 难 (×1.2)'), ('15', '15 难 (×1.3)'),
            ]
            self.difficulty.default = '12'


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
    registration_start = StringField('报名开始时间', validators=[DataRequired(message='请填写报名开始时间')])
    registration_end = StringField('报名截止时间', validators=[DataRequired(message='请填写报名截止时间')])
    tournament_start = StringField('比赛开始时间', validators=[DataRequired(message='请填写比赛开始时间')])
    tournament_end = StringField('比赛结束时间', validators=[DataRequired(message='请填写比赛结束时间')])
    submit = SubmitField('保存设置')


class RuleForm(FlaskForm):
    """规则编辑表单"""
    title = StringField('规则标题', validators=[DataRequired(message='标题不能为空'), Length(max=200)])
    content = TextAreaField('规则内容（支持 HTML）', validators=[Optional()])
    roguelike_key = SelectField('关联肉鸽', choices=[], validators=[Optional()])
    sort_order = StringField('排序', validators=[Optional()], default='0')
    is_published = SelectField('发布状态', choices=[('1', '发布'), ('0', '隐藏')], default='1')
    submit = SubmitField('保存规则')


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
    total_matches = active_match_query().count()
    finished_matches = active_match_query(Match.status == 'finished').count()
    page = request.args.get('page', 1, type=int)
    latest_teams = team_query.order_by(Team.created_at.desc()).paginate(
        page=page, per_page=10)
    carousel_media = load_carousel_media()
    # TOP10 排行榜
    rankings = get_team_rankings()[:10]
    return render_template('index.html',
                           registration_open=setting.registration_open,
                           tournament_status=get_tournament_status(),
                           tournament=tournament,
                           total_teams=total_teams,
                           total_members=total_members,
                           total_matches=total_matches,
                           finished_matches=finished_matches,
                           latest_teams=latest_teams,
                           rankings=rankings,
                           carousel_media=carousel_media)


@main_bp.route('/carousel-media/<path:filename>')
def carousel_media_serve(filename):
    """提供轮播媒体文件（图片/视频），支持子目录"""
    path = os.path.join(BASE_DIR, 'image', filename)
    # 安全检查：防止路径穿越
    real = os.path.realpath(path)
    img_dir = os.path.realpath(os.path.join(BASE_DIR, 'image'))
    if not real.startswith(img_dir):
        abort(404)
    if not os.path.isfile(real):
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
    members = TeamMember.query.filter_by(team_id=team.id).all()
    return render_template('team_detail_public.html', team=team, members=members)


@main_bp.route('/history')
def history():
    """往届赛事归档列表（已结束的赛季，只读）"""
    seasons = Tournament.query.filter_by(is_active=False).order_by(
        Tournament.season_number.desc(), Tournament.id.desc()).all()
    items = []
    for t in seasons:
        rankings = get_team_rankings(t.id)
        items.append({
            'tournament': t,
            'team_count': Team.query.filter_by(tournament_id=t.id).count(),
            'match_count': Match.query.filter_by(tournament_id=t.id, parent_id=None).count(),
            'finished_count': Match.query.filter_by(
                tournament_id=t.id, parent_id=None, status='finished').count(),
            'champion': rankings[0] if rankings else None,
        })
    return render_template('history/index.html', seasons=items)


@main_bp.route('/history/<int:tournament_id>')
def history_detail(tournament_id):
    """往届赛事详情：冠亚季军、队伍积分榜、队员积分榜、比赛结果（只读）"""
    tournament = Tournament.query.get_or_404(tournament_id)
    if tournament.is_active:
        # 当前赛季的数据走赛事大厅/排行榜，避免出现两个入口
        return redirect(url_for('tournament.hall'))
    rankings = get_team_rankings(tournament.id)
    members = TeamMember.query.join(Team).filter(
        Team.tournament_id == tournament.id).order_by(
        TeamMember.final_score.desc()).all()
    matches = Match.query.filter_by(tournament_id=tournament.id, parent_id=None).order_by(
        Match.created_at.desc()).all()
    return render_template('history/detail.html',
                           tournament=tournament,
                           rankings=rankings,
                           members=members,
                           matches=matches,
                           champion=rankings[0] if len(rankings) > 0 else None,
                           runner_up=rankings[1] if len(rankings) > 1 else None,
                           third_place=rankings[2] if len(rankings) > 2 else None)


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
    if not check_registration_open():
        flash('报名已关闭，暂不接受新用户注册', 'warning')
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
    invitation_query = Invitation.query.join(Team, Invitation.team_id == Team.id).filter(
        Invitation.user_id == current_user.id, Invitation.status == 'pending')
    if tournament:
        invitation_query = invitation_query.filter(
            db.or_(Team.tournament_id == tournament.id, Team.tournament_id.is_(None)))
    invitations = invitation_query.all()
    team_membership = current_user.get_team_membership()
    team = team_membership.team if team_membership else None
    return render_template('player/dashboard.html',
                           invitations=invitations,
                           team=team,
                           team_membership=team_membership)


@player_bp.route('/invitations')
@login_required_with_role('player', 'captain')
def invitations():
    page = request.args.get('page', 1, type=int)
    query = Invitation.query.join(Team, Invitation.team_id == Team.id).filter(
        Invitation.user_id == current_user.id)
    tournament = Tournament.get_active()
    if tournament:
        query = query.filter(
            db.or_(Team.tournament_id == tournament.id, Team.tournament_id.is_(None)))
    pagination = query.paginate(page=page, per_page=20)
    return render_template('player/invitations.html',
                           invitations=pagination.items,
                           pagination=pagination)


@player_bp.route('/invitation/<int:invitation_id>/accept', methods=['POST'])
@login_required_with_role('player', 'captain')
def accept_invitation(invitation_id):
    invitation = Invitation.query.get_or_404(invitation_id)
    if invitation.user_id != current_user.id:
        flash('无权限操作', 'danger')
        return redirect(url_for('player.invitations'))
    if invitation.status != 'pending':
        flash('邀请已处理', 'warning')
        return redirect(url_for('player.invitations'))
    # 拒绝跨赛季写入：已归档赛事的队伍不再接受新成员
    if not in_current_season(invitation.team):
        invitation.status = 'rejected'
        db.session.commit()
        flash('该邀请属于已结束的赛事，无法接受', 'warning')
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
    if invitation.user_id != current_user.id:
        flash('无权限操作', 'danger')
        return redirect(url_for('player.invitations'))
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
        chosen = form.roguelike.data
        # 检查 team_limit
        current_count = team_counts.get(chosen, 0)
        limit = roguelike_limits.get(chosen, 1)
        if chosen != team_membership.roguelike and current_count >= limit:
            flash(f'该肉鸽已达到队伍选择上限（{limit}人）', 'danger')
            return redirect(url_for('player.select_roguelike'))
        team_membership.roguelike = chosen
        team_membership.squad = (form.squad.data or '').strip()
        team_membership.operator = (form.operator.data or '').strip()
        if current_user.status < 2:
            current_user.status = 2  # 已选配置
        db.session.commit()
        flash(f'已选择肉鸽：{get_roguelike_name(chosen)}', 'success')
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


@player_bp.route('/profile', methods=['GET', 'POST'])
@login_required_with_role('player', 'captain')
def profile():
    team_membership = current_user.get_team_membership()
    form = DeclarationForm()
    if form.validate_on_submit():
        if not team_membership:
            flash('加入队伍后才能填写参赛宣言', 'warning')
            return redirect(url_for('player.profile'))
        team_membership.declaration = (form.declaration.data or '').strip()
        db.session.commit()
        flash('参赛宣言已更新', 'success')
        return redirect(url_for('player.profile'))
    if request.method == 'GET' and team_membership:
        form.declaration.data = team_membership.declaration or ''
    # 获取比赛历史
    my_matches = Match.query.filter(
        db.or_(Match.player_a_id == current_user.id, Match.player_b_id == current_user.id)
    ).order_by(Match.created_at.desc()).limit(10).all()
    # 获取队员积分
    member_score = team_membership.final_score if team_membership else 0
    return render_template('player/profile.html',
                           team_membership=team_membership,
                           my_matches=my_matches,
                           member_score=member_score,
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
    filepath = os.path.join(UPLOAD_DIR, filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    try:
        file.save(filepath)
    except Exception as e:
        flash(f'头像保存失败: {str(e)}', 'danger')
        return redirect(url_for('player.profile'))
    # 删除旧头像
    if current_user.avatar:
        old_path = os.path.join(UPLOAD_DIR, current_user.avatar)
        if os.path.exists(old_path):
            os.remove(old_path)
    current_user.avatar = filename
    db.session.commit()
    flash('头像上传成功', 'success')
    return redirect(url_for('player.profile'))


@player_bp.route('/time-preferences', methods=['GET', 'POST'])
@login_required_with_role('player', 'captain')
def time_preferences():
    """选手填写可用比赛时间"""
    if not current_user.is_admin() and current_user.status < 2:
        flash('请先选择肉鸽和填写宣言', 'warning')
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
    if current_user.has_team() or current_user.get_team_membership():
        flash('您已在队伍中，不能申请加入其他队伍', 'warning')
        return redirect(url_for('player.dashboard'))
    form = SearchPlayerForm()
    teams = []
    if form.validate_on_submit():
        keyword = form.search_keyword.data
        query = Team.query.filter(Team.team_name.ilike(f'%{keyword}%'))
        tournament = Tournament.get_active()
        if tournament:
            query = query.filter(Team.tournament_id == tournament.id)
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
    if not in_current_season(team):
        flash('该队伍属于已结束的赛事，无法申请加入', 'danger')
        return redirect(url_for('player.search_teams'))
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
    """查看我的申请记录"""
    requests = JoinRequest.query.filter_by(
        user_id=current_user.id
    ).order_by(JoinRequest.created_at.desc()).all()
    return render_template('player/join_requests.html', requests=requests)


# ============================================================
# 队长审核加入申请
# ============================================================


@captain_bp.route('/join-requests')
@login_required_with_role('captain')
def join_requests():
    """队长查看加入申请"""
    team = current_user.team
    if not team:
        flash('您还未创建队伍', 'warning')
        return redirect(url_for('captain.dashboard'))
    requests = JoinRequest.query.filter_by(
        team_id=team.id
    ).order_by(JoinRequest.created_at.desc()).all()
    return render_template('captain/join_requests.html',
                           requests=requests, team=team)


@captain_bp.route('/join-request/<int:request_id>/accept', methods=['POST'])
@login_required_with_role('captain')
def accept_join_request(request_id):
    """队长接受加入申请"""
    join_req = JoinRequest.query.get_or_404(request_id)
    team = current_user.team
    if not team or join_req.team_id != team.id:
        flash('无权限操作', 'danger')
        return redirect(url_for('captain.join_requests'))
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
    team = current_user.team
    if not team or join_req.team_id != team.id:
        flash('无权限操作', 'danger')
        return redirect(url_for('captain.join_requests'))
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


@player_bp.route('/api/players/<int:user_id>')
@login_required
def api_player_detail(user_id):
    """获取单个选手详细数据（JSON）"""
    p = User.query.get_or_404(user_id)
    tm = p.get_team_membership()
    # 比赛记录
    matches = Match.query.filter(
        db.or_(Match.player_a_id == p.id, Match.player_b_id == p.id)
    ).order_by(Match.created_at.desc()).limit(50).all()
    match_list = []
    wins = 0
    losses = 0
    draws = 0
    for m in matches:
        if m.status == 'finished':
            if m.winner_id == p.id:
                result_text = 'win'
                wins += 1
            elif m.winner_id is None:
                result_text = 'draw'
                draws += 1
            else:
                result_text = 'loss'
                losses += 1
        else:
            result_text = m.status
        opponent = m.player_b if m.player_a_id == p.id else m.player_a
        match_list.append({
            'id': m.id,
            'roguelike': m.roguelike,
            'difficulty': m.difficulty,
            'opponent': {'id': opponent.id, 'nickname': opponent.nickname} if opponent else None,
            'result': result_text,
            'status': m.status,
            'created_at': m.created_at.isoformat() if m.created_at else None,
        })
    return jsonify({
        'player': {
            'id': p.id,
            'username': p.username,
            'nickname': p.nickname,
            'avatar': url_for('static', filename='uploads/avatars/' + p.avatar, _external=True) if p.avatar else None,
            'role': p.role,
            'created_at': p.created_at.isoformat() if p.created_at else None,
            'team': {
                'id': tm.team_id,
                'name': tm.team.team_name,
                'team_photo': url_for('static', filename='uploads/teams/' + tm.team.team_photo, _external=True) if tm.team.team_photo else None,
                'roguelike': tm.roguelike,
                'squad': tm.squad or '',
                'operator': tm.operator or '',
                'declaration': tm.declaration,
                'final_score': tm.final_score,
            } if tm else None,
        },
        'stats': {
            'wins': wins,
            'losses': losses,
            'draws': draws,
            'total_matches': wins + losses + draws,
        },
        'recent_matches': match_list[:20],
    })


@player_bp.route('/api/players/<name>')
@login_required
def api_player_by_name(name):
    """通过昵称或用户名获取选手数据（JSON）"""
    # 先尝试按昵称查找，再按用户名查找
    p = User.query.filter(
        db.or_(User.nickname == name, User.username == name)
    ).first_or_404()
    tm = p.get_team_membership()
    # 比赛记录
    matches = Match.query.filter(
        db.or_(Match.player_a_id == p.id, Match.player_b_id == p.id)
    ).order_by(Match.created_at.desc()).limit(50).all()
    match_list = []
    wins = 0
    losses = 0
    draws = 0
    for m in matches:
        if m.status == 'finished':
            if m.winner_id == p.id:
                result_text = 'win'
                wins += 1
            elif m.winner_id is None:
                result_text = 'draw'
                draws += 1
            else:
                result_text = 'loss'
                losses += 1
        else:
            result_text = m.status
        opponent = m.player_b if m.player_a_id == p.id else m.player_a
        match_list.append({
            'id': m.id,
            'roguelike': m.roguelike,
            'difficulty': m.difficulty,
            'opponent': {'id': opponent.id, 'nickname': opponent.nickname} if opponent else None,
            'result': result_text,
            'status': m.status,
            'created_at': m.created_at.isoformat() if m.created_at else None,
        })
    return jsonify({
        'player': {
            'id': p.id,
            'username': p.username,
            'nickname': p.nickname,
            'avatar': url_for('static', filename='uploads/avatars/' + p.avatar, _external=True) if p.avatar else None,
            'role': p.role,
            'created_at': p.created_at.isoformat() if p.created_at else None,
            'team': {
                'id': tm.team_id,
                'name': tm.team.team_name,
                'team_photo': url_for('static', filename='uploads/teams/' + tm.team.team_photo, _external=True) if tm.team.team_photo else None,
                'roguelike': tm.roguelike,
                'squad': tm.squad or '',
                'operator': tm.operator or '',
                'declaration': tm.declaration,
                'final_score': tm.final_score,
            } if tm else None,
        },
        'stats': {
            'wins': wins,
            'losses': losses,
            'draws': draws,
            'total_matches': wins + losses + draws,
        },
        'recent_matches': match_list[:20],
    })


# ============================================================
# Captain 路由
# ============================================================


@captain_bp.route('/dashboard')
@login_required_with_role('captain')
def dashboard():
    team = current_user.team
    if not team:
        return render_template('captain/dashboard.html', team=None)
    members = TeamMember.query.filter_by(team_id=team.id).all()
    pending_invitations = Invitation.query.filter_by(
        team_id=team.id, status='pending').all()
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
    if not check_registration_open():
        flash('报名已关闭，暂不接受创建队伍', 'warning')
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
        flash(f'队伍 【{team.team_name}】 创建成功！您已自动成为队员，请选择肉鸽和位置', 'success')
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
    team = current_user.team
    if not team:
        flash('您还未创建队伍', 'warning')
        return redirect(url_for('captain.dashboard'))
    if team.is_full():
        flash('队伍已满员，不能再邀请成员', 'warning')
        return redirect(url_for('captain.dashboard'))
    if not check_registration_open():
        flash('报名已关闭，暂不接受邀请成员', 'warning')
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
    team = current_user.team
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
    team = current_user.team
    if not team:
        flash('您还未创建队伍', 'warning')
        return redirect(url_for('captain.dashboard'))
    page = request.args.get('page', 1, type=int)
    pagination = Invitation.query.filter_by(team_id=team.id).paginate(
        page=page, per_page=20)
    return render_template('captain/invitations.html',
                           invitations=pagination.items,
                           pagination=pagination, team=team)


@captain_bp.route('/invitation/<int:invitation_id>/cancel', methods=['POST'])
@login_required_with_role('captain')
def cancel_invitation(invitation_id):
    invitation = Invitation.query.get_or_404(invitation_id)
    if invitation.team.captain_id != current_user.id:
        flash('无权限操作', 'danger')
        return redirect(url_for('captain.invitations'))
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
    """队长个人资料页面"""
    team = current_user.team
    my_membership = None
    member_score = 0
    if team:
        my_membership = TeamMember.query.filter_by(
            team_id=team.id, user_id=current_user.id).first()
        member_score = my_membership.final_score if my_membership else 0
    # 比赛记录
    my_matches = Match.query.filter(
        db.or_(Match.player_a_id == current_user.id,
               Match.player_b_id == current_user.id)
    ).order_by(Match.created_at.desc()).limit(10).all()
    return render_template('captain/profile.html',
                           team=team,
                           my_membership=my_membership,
                           member_score=member_score,
                           my_matches=my_matches)


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
    filepath = os.path.join(UPLOAD_DIR, filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    try:
        file.save(filepath)
    except Exception as e:
        flash(f'头像保存失败: {str(e)}', 'danger')
        return redirect(url_for('captain.dashboard'))
    # 删除旧头像
    if current_user.avatar:
        old_path = os.path.join(UPLOAD_DIR, current_user.avatar)
        if os.path.exists(old_path):
            os.remove(old_path)
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
    filepath = os.path.join(TEAM_UPLOAD_DIR, filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    try:
        file.save(filepath)
    except Exception as e:
        flash(f'头像保存失败: {str(e)}', 'danger')
        return redirect(url_for('captain.dashboard'))
    # 删除旧队伍头像
    if team.team_photo:
        old_path = os.path.join(TEAM_UPLOAD_DIR, team.team_photo)
        if os.path.exists(old_path):
            os.remove(old_path)
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
    members = TeamMember.query.filter_by(team_id=t.id).all()
    return jsonify({
        'team': {
            'id': t.id,
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
    team_query = Team.query.filter_by(tournament_id=tournament.id) if tournament else Team.query
    total_users = User.query.count()
    total_teams = team_query.count()
    total_captains = User.query.filter_by(role='captain').count()
    total_players = User.query.filter_by(role='player').count()
    total_members = TeamMember.query.join(Team).filter(
        Team.tournament_id == tournament.id).count() if tournament else TeamMember.query.count()
    latest_teams = team_query.order_by(Team.created_at.desc()).limit(5).all()
    setting = Setting.get_instance()
    # 赛事统计
    match_query = Match.query.filter(Match.parent_id.is_(None))
    if tournament:
        match_query = match_query.filter(Match.tournament_id == tournament.id)
    total_matches = match_query.count()
    pending_matches = match_query.filter(Match.status == 'pending').count()
    finished_matches = match_query.filter(Match.status == 'finished').count()
    return render_template('admin/dashboard.html',
                           total_users=total_users,
                           total_teams=total_teams,
                           total_captains=total_captains,
                           total_players=total_players,
                           total_members=total_members,
                           latest_teams=latest_teams,
                           registration_open=setting.registration_open,
                           tournament_status=get_tournament_status(),
                           tournament=tournament,
                           total_matches=total_matches,
                           pending_matches=pending_matches,
                           finished_matches=finished_matches)


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
    flash(f'已将 【{user.nickname}】 降级为选手', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/user/<int:user_id>/reset-password', methods=['POST'])
@login_required_with_role('admin')
def reset_user_password(user_id):
    """管理员重置用户密码为临时密码（下次登录强制修改）"""
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
    flash(f'已重置 【{user.nickname}】 的密码，临时密码：{temp_password}（该用户下次登录将被强制修改）', 'success')
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
    members = TeamMember.query.filter_by(team_id=team.id).all()
    return render_template('admin/team_detail.html', team=team, members=members)


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
    flash(f'已删除队伍 【{team_name}】', 'success')
    return redirect(url_for('admin.teams'))


@admin_bp.route('/team/<int:team_id>/update-scores', methods=['POST'])
@login_required_with_role('admin')
def update_team_scores(team_id):
    """管理员手动更新队员积分和队伍积分"""
    team = Team.query.get_or_404(team_id)
    members = TeamMember.query.filter_by(team_id=team.id).all()

    updated_count = 0
    for member in members:
        score_key = f'score_{member.id}'
        score_value = request.form.get(score_key, '').strip()
        if score_value:
            try:
                new_score = int(score_value)
                if new_score < 0:
                    new_score = 0
                member.final_score = new_score
                updated_count += 1
            except ValueError:
                flash(f'队员 {member.user.nickname} 的积分格式无效，已跳过', 'warning')
                continue

    # 自动重算队伍总积分 = 队员积分之和 × 加成倍率
    tournament = Tournament.get_active()
    bonus = tournament.bonus_score if tournament else 500
    team.team_score = sum(m.final_score or 0 for m in members) * bonus

    db.session.commit()
    flash(f'已更新 {updated_count} 名队员的积分，队伍总积分已自动重算', 'success')
    return redirect(url_for('admin.team_detail', team_id=team.id))


@admin_bp.route('/team/<int:team_id>/update-member/<int:member_id>', methods=['POST'])
@login_required_with_role('admin')
def update_member_info(team_id, member_id):
    """管理员编辑队员信息：头像、肉鸽、干员、宣言、结算分"""
    member = TeamMember.query.get_or_404(member_id)
    if member.team_id != team_id:
        flash('该成员不属于此队伍', 'danger')
        return redirect(url_for('admin.teams'))

    # 更新头像
    if 'avatar' in request.files and request.files['avatar'].filename:
        file = request.files['avatar']
        allowed_ext = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
        ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
        if ext in allowed_ext:
            filename = f'{uuid.uuid4().hex}.{ext}'
            filepath = os.path.join(UPLOAD_DIR, filename)
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            try:
                file.save(filepath)
            except Exception as e:
                flash(f'头像保存失败: {str(e)}', 'danger')
                return redirect(url_for('admin.team_detail', team_id=team_id))
            if member.user.avatar:
                old = os.path.join(UPLOAD_DIR, member.user.avatar)
                if os.path.exists(old):
                    os.remove(old)
            member.user.avatar = filename

    # 更新肉鸽
    roguelike = request.form.get('roguelike', '').strip()
    if roguelike:
        member.roguelike = roguelike

    # 更新分队 & 干员
    member.squad = (request.form.get('squad', '') or '').strip()
    member.operator = (request.form.get('operator', '') or '').strip()

    # 更新宣言
    declaration = request.form.get('declaration', '').strip()
    if declaration:
        member.declaration = declaration

    # 更新结算分（手动覆盖，最高优先级）
    manual_score = request.form.get('final_score', '').strip()
    if manual_score:
        try:
            member.final_score = int(manual_score)
        except ValueError:
            flash(f'结算分格式无效: {manual_score}', 'warning')

    # 更新队伍宣言
    team_slogan = request.form.get('team_slogan', '').strip()
    if team_slogan:
        team = Team.query.get(team_id)
        if team:
            team.team_slogan = team_slogan

    db.session.commit()
    log_system_action('update_member_info', f'管理员更新队员 {member.user.nickname} 的信息')
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
            filepath = os.path.join(TEAM_UPLOAD_DIR, filename)
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            try:
                file.save(filepath)
            except Exception as e:
                flash(f'头像保存失败: {str(e)}', 'danger')
                return redirect(url_for('admin.team_detail', team_id=team_id))
            if team.team_photo:
                old = os.path.join(TEAM_UPLOAD_DIR, team.team_photo)
                if os.path.exists(old):
                    os.remove(old)
            team.team_photo = filename

    db.session.commit()
    log_system_action('update_team_info', f'管理员更新队伍 {team.team_name} 信息')
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
    return render_template('admin/roguelikes.html', roguelikes=all_roguelikes)


@admin_bp.route('/roguelike/add', methods=['POST'])
@login_required_with_role('admin')
def add_roguelike():
    """新增肉鸽"""
    key = request.form.get('key', '').strip()
    name = request.form.get('name', '').strip()
    max_per_team = request.form.get('max_per_team', '1')
    sort_order = request.form.get('sort_order', '0')
    diff_min = request.form.get('difficulty_min', '12')
    diff_max = request.form.get('difficulty_max', '18')
    diff_step = request.form.get('difficulty_step', '0.1')
    if not key or not name:
        flash('标识和名称不能为空', 'danger')
        return redirect(url_for('admin.roguelikes'))
    if Roguelike.query.filter_by(key=key).first():
        flash(f'肉鸽标识 "{key}" 已存在', 'danger')
        return redirect(url_for('admin.roguelikes'))
    try:
        rl = Roguelike(
            key=key, name=name,
            max_per_team=int(max_per_team),
            sort_order=int(sort_order),
            difficulty_min=int(diff_min),
            difficulty_max=int(diff_max),
            difficulty_step=float(diff_step),
            is_active=True
        )
        db.session.add(rl)
        db.session.commit()
        flash(f'肉鸽 "{name}" 已添加', 'success')
    except ValueError:
        flash('人数限制、排序和难度配置必须为数字', 'danger')
    return redirect(url_for('admin.roguelikes'))


@admin_bp.route('/roguelike/<int:rl_id>/edit', methods=['POST'])
@login_required_with_role('admin')
def edit_roguelike(rl_id):
    """编辑肉鸽"""
    rl = Roguelike.query.get_or_404(rl_id)
    rl.name = request.form.get('name', rl.name).strip()
    rl.key = request.form.get('key', rl.key).strip()
    try:
        rl.max_per_team = int(request.form.get('max_per_team', rl.max_per_team))
        rl.sort_order = int(request.form.get('sort_order', rl.sort_order))
        rl.difficulty_min = int(request.form.get('difficulty_min', rl.difficulty_min))
        rl.difficulty_max = int(request.form.get('difficulty_max', rl.difficulty_max))
        rl.difficulty_step = float(request.form.get('difficulty_step', rl.difficulty_step))
    except ValueError:
        flash('人数限制、排序和难度配置必须为数字', 'danger')
        return redirect(url_for('admin.roguelikes'))
    rl.is_active = request.form.get('is_active') == '1'
    db.session.commit()
    flash(f'肉鸽 "{rl.name}" 已更新', 'success')
    return redirect(url_for('admin.roguelikes'))


@admin_bp.route('/roguelike/<int:rl_id>/delete', methods=['POST'])
@login_required_with_role('admin')
def delete_roguelike(rl_id):
    """删除肉鸽"""
    rl = Roguelike.query.get_or_404(rl_id)
    name = rl.name
    db.session.delete(rl)
    db.session.commit()
    flash(f'肉鸽 "{name}" 已删除', 'success')
    return redirect(url_for('admin.roguelikes'))


# ============================================================
# Admin 轮播图管理路由
# ============================================================


@admin_bp.route('/carousel')
@login_required_with_role('admin')
def carousel():
    """轮播图管理列表"""
    tournament = Tournament.get_active()
    items = []
    if tournament:
        items = CarouselItem.query.filter_by(
            tournament_id=tournament.id
        ).order_by(CarouselItem.sort_order).all()
    return render_template('admin/carousel.html', items=items, tournament=tournament)


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
    flash(f'轮播图 "{name}" 已删除', 'success')
    return redirect(url_for('admin.carousel'))


@admin_bp.route('/registration')
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
                           form=form, registration_open=setting.registration_open)


@admin_bp.route('/export')
@login_required_with_role('admin')
def export_excel():
    tournament = Tournament.get_active()
    teams = (Team.query.filter_by(tournament_id=tournament.id).all()
             if tournament else Team.query.all())
    team_size = get_team_size()
    wb = Workbook()
    ws = wb.active
    ws.title = '报名信息'
    headers = (['队伍名称', '队长昵称'] +
               [f'成员{i + 1}' for i in range(team_size)] +
               ['肉鸽选择', '参赛宣言', '个人积分'])
    ws.append(headers)
    for team in teams:
        members = TeamMember.query.filter_by(team_id=team.id).all()
        row = [team.team_name, team.captain.nickname]
        # 队长也是队员，显示所有队员
        for i in range(team_size):
            row.append(members[i].user.nickname if i < len(members) else '')
        # 肉鸽选择
        roguelikes = []
        for m in members:
            if m.roguelike:
                rl_name = get_roguelike_name(m.roguelike)
                roguelikes.append(f'{m.user.nickname}: {rl_name}')
        # 参赛宣言
        declarations = [f'{m.user.nickname}: {m.declaration}' for m in members if m.declaration]
        # 个人积分
        scores = [f'{m.user.nickname}: {m.final_score}' for m in members]
        row.append('\n'.join(roguelikes) if roguelikes else '')
        row.append('\n'.join(declarations) if declarations else '')
        row.append('\n'.join(scores) if scores else '')
        ws.append(row)
    for column in ws.columns:
        max_length = 0
        col_letter = column[0].column_letter
        for cell in column:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:  # noqa: E722
                pass
        ws.column_dimensions[col_letter].width = max_length + 2
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    filename = f'{get_event_name()}报名信息_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
    return send_file(output,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=filename)


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
    form.roguelike_key.choices = [('', '-- 主规则（不关联肉鸽）--')] + [(r.key, r.name) for r in all_roguelikes]
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

    # 验证所有队伍是否满足参赛条件
    setting = Setting.get_instance()
    teams = Team.query.filter_by(tournament_id=tournament.id).all()
    team_size = get_team_size()
    errors = []
    for team in teams:
        members = TeamMember.query.filter_by(team_id=team.id).all()
        # 检查队伍人数（开启「允许未满员开赛」时跳过）
        if len(members) < team_size and not setting.allow_incomplete_teams:
            errors.append(f'队伍「{team.team_name}」人数不足（{len(members)}/{team_size}）')
        # 检查队员是否都选了肉鸽
        for m in members:
            if not m.roguelike:
                errors.append(f'队伍「{team.team_name}」的 {m.user.nickname} 未选择肉鸽')

    if errors:
        flash('以下队伍不满足参赛条件：<br>' + '<br>'.join(errors), 'danger')
        return redirect(url_for('admin.dashboard'))

    # 锁定报名
    setting.registration_open = False
    tournament.status = 'matching'
    db.session.commit()
    log_system_action('start_tournament', '赛事进入匹配阶段')
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
    # 执行自动匹配
    result = _auto_match()
    if result['success']:
        tournament.status = 'running'
        db.session.commit()
        log_system_action('run_matching', f'自动匹配完成，生成 {result["count"]} 场比赛')
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
            setting.advance_count = int(form.advance_count.data)
            if tournament:
                tournament.bonus_score = int(form.bonus_score.data)
            db.session.commit()
            flash('积分配置已更新', 'success')
        except ValueError:
            flash('请输入有效的数字', 'danger')
        return redirect(url_for('admin.score_config'))
    elif request.method == 'GET':
        form.bonus_score.data = str(tournament.bonus_score if tournament else 500)
        form.advance_count.data = str(setting.advance_count if setting.advance_count else 4)
    return render_template('admin/score_config.html', form=form, setting=setting, tournament=tournament)


@admin_bp.route('/reset-scores', methods=['POST'])
@login_required_with_role('admin')
def reset_scores():
    """积分重置：清零所有积分并从已完成比赛重新计算"""
    recalculate_all_scores()
    flash('积分已重置并重新计算', 'success')
    return redirect(url_for('admin.score_config'))


@admin_bp.route('/tournament-settings', methods=['GET', 'POST'])
@login_required_with_role('admin')
def tournament_settings():
    """赛事设置（名称 + 报名/比赛时间）"""
    tournament = Tournament.get_active()
    setting = Setting.get_instance()
    form = TournamentSettingsForm()
    if form.validate_on_submit():
        try:
            if not tournament:
                tournament = Tournament(
                    name=current_app.config['TOURNAMENT_NAME'],
                    season_number=current_app.config['SEASON_NUMBER'],
                    is_active=True)
                db.session.add(tournament)
            new_name = (form.tournament_name.data or '').strip()
            if new_name:
                setting.tournament_name = new_name
                tournament.name = new_name
            # 队伍人数：留空（None）表示回退环境变量 TEAM_SIZE
            setting.team_size = form.team_size.data
            setting.allow_incomplete_teams = bool(form.allow_incomplete_teams.data)
            tournament.registration_start = datetime.strptime(form.registration_start.data, '%Y-%m-%dT%H:%M')
            tournament.registration_end = datetime.strptime(form.registration_end.data, '%Y-%m-%dT%H:%M')
            tournament.tournament_start = datetime.strptime(form.tournament_start.data, '%Y-%m-%dT%H:%M')
            tournament.tournament_end = datetime.strptime(form.tournament_end.data, '%Y-%m-%dT%H:%M')
            db.session.commit()
            flash('赛事设置已更新', 'success')
        except ValueError:
            flash('时间格式错误，请使用正确的日期时间格式', 'danger')
        return redirect(url_for('admin.tournament_settings'))
    elif request.method == 'GET':
        form.tournament_name.data = setting.tournament_name or (
            tournament.name if tournament else current_app.config['TOURNAMENT_NAME'])
        form.team_size.data = setting.team_size
        form.allow_incomplete_teams.data = bool(setting.allow_incomplete_teams)
        if tournament:
            if tournament.registration_start:
                form.registration_start.data = tournament.registration_start.strftime('%Y-%m-%dT%H:%M')
            if tournament.registration_end:
                form.registration_end.data = tournament.registration_end.strftime('%Y-%m-%dT%H:%M')
            if tournament.tournament_start:
                form.tournament_start.data = tournament.tournament_start.strftime('%Y-%m-%dT%H:%M')
            if tournament.tournament_end:
                form.tournament_end.data = tournament.tournament_end.strftime('%Y-%m-%dT%H:%M')
    return render_template('admin/tournament_settings.html', form=form, tournament=tournament,
                           env_team_size=current_app.config['TEAM_SIZE'])


@admin_bp.route('/new-season', methods=['POST'])
@login_required_with_role('admin')
def new_season():
    """归档当前赛事并开启新赛季（旧数据保留可查）"""
    setting = Setting.get_instance()
    current = Tournament.get_active()
    season_number = current.season_number + 1 if current else 1
    name = (request.form.get('season_name') or '').strip()
    if not name:
        name = f"{current_app.config['EVENT_NAME']} #{season_number}"
    if current:
        current.is_active = False
        if current.status != 'finished':
            current.status = 'finished'
        # 清理上届遗留的待处理邀请/申请，避免新赛季误操作到已归档队伍
        archived_team_ids = [t.id for t in Team.query.filter_by(tournament_id=current.id).all()]
        if archived_team_ids:
            Invitation.query.filter(
                Invitation.team_id.in_(archived_team_ids),
                Invitation.status == 'pending').delete(synchronize_session=False)
            JoinRequest.query.filter(
                JoinRequest.team_id.in_(archived_team_ids),
                JoinRequest.status == 'pending').delete(synchronize_session=False)
    new_tournament = Tournament(
        name=name, season_number=season_number, is_active=True,
        status='registration',
        bonus_score=current.bonus_score if current else current_app.config['DEFAULT_BONUS_SCORE'])
    db.session.add(new_tournament)
    # 参赛进度随赛季归零；管理员角色与已完成的新手引导保留
    User.query.filter(User.role.notin_(['admin', 'super_admin'])).update(
        {'status': 0}, synchronize_session=False)
    TimePreference.query.delete(synchronize_session=False)
    setting.tournament_name = name
    setting.advance_count = 0
    setting.registration_open = True
    db.session.commit()
    log_system_action('new_season', f'开启新赛季：{name}')
    flash(f'已开启新赛季【{name}】，上届数据已归档保留，用户需重新组队', 'success')
    return redirect(url_for('admin.tournament_settings'))


@admin_bp.route('/advance')
@login_required_with_role('admin')
def advance():
    """晋级管理"""
    setting = Setting.get_instance()
    rankings = get_team_rankings()
    advanced = get_advanced_teams()
    return render_template('admin/advance.html',
                           setting=setting,
                           rankings=rankings, advanced=advanced)


@admin_bp.route('/finish-tournament', methods=['POST'])
@login_required_with_role('admin')
def finish_tournament():
    """结束赛事"""
    tournament = Tournament.get_active()
    if not tournament or tournament.status != 'running':
        flash('当前状态不允许结束赛事', 'warning')
        return redirect(url_for('admin.dashboard'))
    tournament.status = 'finished'
    tournament.tournament_end = datetime.utcnow()
    db.session.commit()
    log_system_action('finish_tournament', '赛事已结束')
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
    setting = Setting.get_instance()
    setting.registration_open = True
    tournament.status = 'registration'
    db.session.commit()
    log_system_action('resume_registration', '恢复报名阶段')
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
    query = Match.query
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
    match = Match.query.get_or_404(match_id)
    if match.status != 'finished':
        flash('只能审核已完成的比赛', 'warning')
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
    """管理员修改比赛结果（记录日志）"""
    match = Match.query.get_or_404(match_id)
    if match.status not in ('finished', 'forfeit_a', 'forfeit_b', 'submitted'):
        flash('只能修改已提交或已结束的比赛', 'warning')
        return redirect(url_for('admin.matches'))
    action = request.form.get('action')
    reason = request.form.get('reason', '')
    score_a = request.form.get('score_a', type=int)
    score_b = request.form.get('score_b', type=int)
    diff_a = request.form.get('diff_a', type=int)
    diff_b = request.form.get('diff_b', type=int)

    # 记录旧结果
    old_result = f'A:{match.player_a_result} B:{match.player_b_result} winner:{match.winner_id} score_a:{match.player_a_score} score_b:{match.player_b_score} diff_a:{match.get_player_a_difficulty()} diff_b:{match.get_player_b_difficulty()}'

    if action == 'win_a':
        match.winner_id = match.player_a_id
        match.player_a_result = 'win'
        match.player_b_result = 'lose'
        match.status = 'finished'
    elif action == 'win_b':
        match.winner_id = match.player_b_id
        match.player_a_result = 'lose'
        match.player_b_result = 'win'
        match.status = 'finished'
    elif action == 'draw':
        match.winner_id = None
        match.player_a_result = 'draw'
        match.player_b_result = 'draw'
        match.status = 'finished'
    else:
        flash('无效操作', 'danger')
        return redirect(url_for('admin.matches'))

    # 更新分数
    if score_a is not None:
        match.player_a_score = score_a
    if score_b is not None:
        match.player_b_score = score_b
    # 更新双方独立难度
    if diff_a is not None:
        match.player_a_difficulty = diff_a
    if diff_b is not None:
        match.player_b_difficulty = diff_b

    new_result = f'A:{match.player_a_result} B:{match.player_b_result} winner:{match.winner_id} score_a:{match.player_a_score} score_b:{match.player_b_score} diff_a:{match.get_player_a_difficulty()} diff_b:{match.get_player_b_difficulty()}'

    # 记录修改日志
    log = ScoreLog(
        match_id=match.id,
        modified_by=current_user.id,
        old_result=old_result,
        new_result=new_result,
        reason=reason
    )
    db.session.add(log)

    # 重新计算积分（内部会 commit）
    _update_team_scores(match)

    flash('比赛结果已修改，积分已重新计算', 'success')
    return redirect(url_for('admin.matches'))


@admin_bp.route('/match/<int:match_id>/score-logs')
@login_required_with_role('admin')
def match_score_logs(match_id):
    """查看比赛成绩修改日志"""
    match = Match.query.get_or_404(match_id)
    logs = ScoreLog.query.filter_by(match_id=match_id).order_by(ScoreLog.created_at.desc()).all()
    return render_template('admin/score_logs.html', match=match, logs=logs)


# ============================================================
# Admin 手动匹配 & 日程管理
# ============================================================

def _parse_duration(note):
    """从 match_note 解析时长（分钟）"""
    if note and 'duration:' in note:
        try:
            # 格式: duration:120 或 duration:120|host:xxx
            dur_part = note.split('duration:')[1].split('|')[0]
            return int(dur_part)
        except (ValueError, IndexError):
            pass
    return None


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
                           roguelikes=get_active_roguelikes())


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
        Match.scheduled_time.asc().nullslast(), Match.created_at.asc()
    ).all()
    result = []
    for m in matches:
        children_data = []
        for child in sorted(m.children, key=lambda c: c.scheduled_time or datetime.max):
            if child.scheduled_time:
                dur = _parse_duration(child.match_note) or current_app.config['SCHEDULE_SLOT_MINUTES']
                children_data.append({
                    'id': child.id,
                    'parent_id': m.id,
                    'start': child.scheduled_time.isoformat(),
                    'end': (child.scheduled_time + timedelta(minutes=dur)).isoformat(),
                    'className': m.status,
                    'editable': True,
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
            'scheduled_time': m.scheduled_time.isoformat() if m.scheduled_time else None,
            'duration_minutes': 0,
            'is_exhibition': False,
            'children': children_data,
        })
    return jsonify({'matches': result})


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

    # 如果用户填了日期，用它；否则默认今天
    date_str = request.form.get('match_date', '').strip()
    if date_str:
        try:
            base_date = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            base_date = datetime.now()
    else:
        base_date = datetime.now()

    base_date = base_date.replace(hour=0, minute=0, second=0, microsecond=0)
    slot_times = _parse_slot_times(base_date)
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
    for index, slot_time in enumerate(slot_times, start=1):
        swap = index % 2 == 0
        db.session.add(Match(
            tournament_id=match.tournament_id,
            player_a_id=player_b_id if swap else player_a_id,
            player_b_id=player_a_id if swap else player_b_id,
            team_a_id=mb.team_id if swap else ma.team_id,
            team_b_id=ma.team_id if swap else mb.team_id,
            roguelike=roguelike, status='pending',
            parent_id=match.id,
            scheduled_time=slot_time,
            match_note=duration_note,
            segment_order=index,
        ))
    db.session.commit()

    slot_text = ' & '.join(t.strftime('%H:%M') for t in slot_times)
    log_system_action('manual_match',
                      f'创建比赛 {match.id}: {ma.user.nickname} vs {mb.user.nickname}（{len(slot_times)} 个时段）')
    flash(f'比赛已创建！{ma.user.nickname} vs {mb.user.nickname}（{slot_text}）', 'success')
    return redirect(url_for('admin.schedule'))


@admin_bp.route('/schedule/update-time', methods=['POST'])
@login_required_with_role('admin')
def schedule_update_time():
    """更新比赛时间（拖拽日程后调用）"""
    match_id = request.form.get('match_id', type=int)
    scheduled_time_str = request.form.get('scheduled_time', '').strip()

    match = Match.query.get_or_404(match_id)
    if scheduled_time_str:
        try:
            match.scheduled_time = datetime.strptime(scheduled_time_str, '%Y-%m-%dT%H:%M')
        except ValueError:
            return jsonify({'error': '时间格式无效'}), 400
    else:
        match.scheduled_time = None

    # 更新时长
    duration = request.form.get('duration_minutes', type=int)
    if duration:
        match.match_note = f'duration:{duration}'
    elif not match.match_note or not match.match_note.startswith('duration:'):
        match.match_note = f'duration:60'

    db.session.commit()
    return jsonify({'success': True, 'match_id': match.id})


@admin_bp.route('/schedule/split-match', methods=['POST'])
@login_required_with_role('admin')
def schedule_split_match():
    """剪断比赛：把一场比赛切分成多个独立时段，每段不同主次选手"""
    match_id = request.form.get('match_id', type=int)
    parts = request.form.get('parts', type=int) or 2

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
                scheduled_time=seg_start,
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
            scheduled_time = datetime.strptime(scheduled_time_str, '%Y-%m-%dT%H:%M')
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
        scheduled_time=scheduled_time,
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
        return jsonify({'error': '请先在左侧选择肉鸽'}), 400

    scheduled_time = None
    if scheduled_time_str:
        try:
            scheduled_time = datetime.strptime(scheduled_time_str, '%Y-%m-%dT%H:%M')
        except ValueError:
            pass

    match = Match(
        tournament_id=Tournament.get_active().id if Tournament.get_active() else None,
        player_a_id=User.query.filter_by(role='admin').first().id if User.query.filter_by(role='admin').first() else 1,
        player_b_id=User.query.filter_by(role='admin').first().id if User.query.filter_by(role='admin').first() else 1,
        team_a_id=None, team_b_id=None,
        roguelike=roguelike,
        status='pending',
        scheduled_time=scheduled_time,
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
    flash(f'已删除比赛: {info}', 'info')
    return redirect(url_for('admin.schedule'))


@admin_bp.route('/schedule/confirm-match', methods=['POST'])
@login_required_with_role('admin')
def schedule_confirm_match():
    """管理员一键确认比赛（跳过 pending→scheduled→confirmed 流程）"""
    match_id = request.form.get('match_id', type=int)
    match = Match.query.get_or_404(match_id)
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
    setting = Setting.get_instance()
    tournament = Tournament.get_active()
    total_matches = active_match_query().count()
    pending_matches = active_match_query(Match.status == 'pending').count()
    scheduled_matches = active_match_query(Match.status == 'scheduled').count()
    confirmed_matches = active_match_query(Match.status == 'confirmed').count()
    submitted_matches = active_match_query(Match.status == 'submitted').count()
    finished_matches = active_match_query(Match.status == 'finished').count()
    rankings = get_team_rankings()
    advanced = get_advanced_teams()
    recent_matches = active_match_query().order_by(Match.created_at.desc()).limit(30).all()
    return render_template('tournament/hall.html',
                           setting=setting,
                           tournament=tournament,
                           tournament_status=get_tournament_status(),
                           total_matches=total_matches,
                           pending_matches=pending_matches,
                           scheduled_matches=scheduled_matches,
                           confirmed_matches=confirmed_matches,
                           submitted_matches=submitted_matches,
                           finished_matches=finished_matches,
                           rankings=rankings,
                           advanced=advanced,
                           recent_matches=recent_matches)


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
    champion = rankings[0] if len(rankings) > 0 else None
    runner_up = rankings[1] if len(rankings) > 1 else None
    third_place = rankings[2] if len(rankings) > 2 else None
    return render_template('tournament/results.html',
                           tournament=tournament,
                           tournament_status=get_tournament_status(),
                           champion=champion,
                           runner_up=runner_up,
                           third_place=third_place,
                           rankings=rankings)


@tournament_bp.route('/my-matches')
@login_required_with_role('player', 'captain')
def my_matches():
    """我的比赛——只显示父比赛"""
    user_id = current_user.id
    matches = Match.query.filter(
        Match.parent_id == None,
        db.or_(Match.player_a_id == user_id, Match.player_b_id == user_id)
    ).order_by(Match.created_at.desc()).all()
    return render_template('tournament/my_matches.html', matches=matches)


@tournament_bp.route('/match/<int:match_id>')
@login_required_with_role('player', 'captain', 'admin')
def match_detail(match_id):
    """比赛详情"""
    match = Match.query.get_or_404(match_id)
    is_participant = (current_user.id == match.player_a_id or
                      current_user.id == match.player_b_id)
    if not current_user.is_admin() and not is_participant:
        flash('您没有权限查看此比赛', 'danger')
        return redirect(url_for('tournament.my_matches'))
    return render_template('tournament/match_detail.html',
                           match=match,
                           is_participant=is_participant)


@tournament_bp.route('/match/<int:match_id>/schedule', methods=['GET', 'POST'])
@login_required_with_role('player', 'captain')
def schedule_match(match_id):
    """确认比赛时间（线下商讨后在此确定）"""
    match = Match.query.get_or_404(match_id)
    if current_user.id not in (match.player_a_id, match.player_b_id):
        flash('无权限操作', 'danger')
        return redirect(url_for('tournament.my_matches'))
    if match.status not in ('pending', 'scheduled'):
        flash('当前状态不允许修改时间', 'warning')
        return redirect(url_for('tournament.match_detail', match_id=match_id))

    is_a = current_user.id == match.player_a_id
    already_confirmed = match.confirmed_a if is_a else match.confirmed_b

    form = MatchScheduleForm()
    if form.validate_on_submit():
        try:
            scheduled_time = datetime.strptime(form.scheduled_time.data, '%Y-%m-%d %H:%M')
            match.scheduled_time = scheduled_time
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
    match = Match.query.get_or_404(match_id)
    if current_user.id not in (match.player_a_id, match.player_b_id):
        flash('无权限操作', 'danger')
        return redirect(url_for('tournament.my_matches'))
    if match.status not in ('scheduled',):
        flash('当前状态不允许确认时间', 'warning')
        return redirect(url_for('tournament.match_detail', match_id=match_id))
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
    """弃权比赛"""
    match = Match.query.get_or_404(match_id)
    if current_user.id not in (match.player_a_id, match.player_b_id):
        flash('无权限操作', 'danger')
        return redirect(url_for('tournament.my_matches'))
    if match.status in ('finished', 'dispute', 'forfeit_a', 'forfeit_b'):
        flash('比赛已结束，无法弃权', 'warning')
        return redirect(url_for('tournament.match_detail', match_id=match_id))
    is_a = current_user.id == match.player_a_id
    if is_a:
        match.status = 'forfeit_a'
        match.winner_id = match.player_b_id
        match.player_a_result = 'forfeit'
        match.player_b_result = 'win'
    else:
        match.status = 'forfeit_b'
        match.winner_id = match.player_a_id
        match.player_a_result = 'win'
        match.player_b_result = 'forfeit'
    match.finished_at = datetime.utcnow()
    # 更新队伍积分（内部会 commit）
    _update_team_scores(match)
    flash('已弃权该场比赛', 'info')
    return redirect(url_for('tournament.match_detail', match_id=match_id))


@tournament_bp.route('/match/<int:match_id>/note', methods=['POST'])
@login_required_with_role('player', 'captain')
def send_match_note(match_id):
    """发送比赛留言"""
    match = Match.query.get_or_404(match_id)
    if current_user.id not in (match.player_a_id, match.player_b_id):
        flash('无权限操作', 'danger')
        return redirect(url_for('tournament.my_matches'))
    form = MatchNoteForm()
    if form.validate_on_submit():
        note = form.match_note.data
        if match.match_note:
            match.match_note += f'\n[{datetime.now().strftime("%m-%d %H:%M")}] {current_user.nickname}: {note}'
        else:
            match.match_note = f'[{datetime.now().strftime("%m-%d %H:%M")}] {current_user.nickname}: {note}'
        db.session.commit()
        flash('留言已发送', 'success')
    return redirect(url_for('tournament.match_detail', match_id=match_id))


@tournament_bp.route('/match/<int:match_id>/submit-result', methods=['GET', 'POST'])
@login_required_with_role('player', 'captain')
def submit_result(match_id):
    """提交比赛结果——上传截图 + 难度"""
    match = Match.query.get_or_404(match_id)
    if current_user.id not in (match.player_a_id, match.player_b_id):
        flash('无权限操作', 'danger')
        return redirect(url_for('tournament.my_matches'))
    if match.status not in ('confirmed', 'submitted'):
        flash('比赛已结束或存在争议', 'warning')
        return redirect(url_for('tournament.match_detail', match_id=match_id))

    if request.method == 'POST':
        difficulty = request.form.get('difficulty', type=int)
        score = request.form.get('score', type=int)
        if not difficulty:
            flash('请选择难度', 'danger')
            return redirect(url_for('tournament.submit_result', match_id=match_id))

        is_player_a = current_user.id == match.player_a_id

        # 处理图片上传（支持多张）
        if 'screenshots' in request.files:
            files = request.files.getlist('screenshots')
            for file in files:
                if file and file.filename:
                    allowed_ext = {'png', 'jpg', 'jpeg', 'webp'}
                    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
                    if ext in allowed_ext:
                        filename = f'screenshot_{match_id}_{current_user.id}_{uuid.uuid4().hex[:8]}.{ext}'
                        upload_dir = os.path.join(BASE_DIR, 'app', 'static', 'uploads', 'screenshots')
                        os.makedirs(upload_dir, exist_ok=True)
                        filepath = os.path.join(upload_dir, filename)
                        try:
                            file.save(filepath)
                        except Exception as e:
                            flash(f'截图保存失败: {str(e)}', 'danger')
                            return redirect(url_for('tournament.submit_result', match_id=match_id))
                        if match.match_note:
                            match.match_note += f'\n[截图] {current_user.nickname}: /static/uploads/screenshots/{filename}'
                        else:
                            match.match_note = f'[截图] {current_user.nickname}: /static/uploads/screenshots/{filename}'

        if is_player_a:
            if match.player_a_result is not None:
                flash('您已提交过结果', 'warning')
                return redirect(url_for('tournament.match_detail', match_id=match_id))
            match.player_a_difficulty = difficulty
            if score:
                match.player_a_score = score
            match.player_a_result = 'submitted'
        else:
            if match.player_b_result is not None:
                flash('您已提交过结果', 'warning')
                return redirect(url_for('tournament.match_detail', match_id=match_id))
            match.player_b_difficulty = difficulty
            if score:
                match.player_b_score = score
            match.player_b_result = 'submitted'

        # 两人都提交了才变 submitted，否则 confirmed
        if match.player_a_result is not None and match.player_b_result is not None:
            match.status = 'submitted'
        current_user.status = 6
        db.session.commit()
        flash('成绩已提交！', 'success')
        return redirect(url_for('tournament.match_detail', match_id=match_id))

    return render_template('tournament/submit_result.html', match=match)


# ============================================================
# User Loader
# ============================================================


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ============================================================
# 应用工厂
# ============================================================


def _migrate_teams_unique(cursor):
    """把 teams 表的全局唯一约束（队名/队长）重建为「赛事内唯一」

    SQLite 不支持删除表内联 UNIQUE 约束，只能建新表→copy→换名。
    旧数据全部保留；tournament_id 为空的队伍回填到当前赛事，避免 NULL 破坏唯一性。
    """
    row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='teams'").fetchone()
    if not row or not row[0]:
        return
    if 'uq_tournament_captain' in row[0]:
        return  # 已是新结构

    active = cursor.execute(
        "SELECT id FROM tournaments WHERE is_active=1 ORDER BY id LIMIT 1").fetchone()
    if not active:
        active = cursor.execute(
            "SELECT id FROM tournaments ORDER BY id LIMIT 1").fetchone()
    if active:
        cursor.execute("UPDATE teams SET tournament_id=? WHERE tournament_id IS NULL",
                       (active[0],))

    cursor.execute("PRAGMA foreign_keys=OFF")
    cursor.execute("DROP TABLE IF EXISTS teams_new")
    cursor.execute("""
        CREATE TABLE teams_new (
            id INTEGER NOT NULL,
            team_name VARCHAR(50) NOT NULL,
            team_slogan VARCHAR(300),
            captain_id INTEGER NOT NULL,
            tournament_id INTEGER,
            team_photo VARCHAR(500),
            team_score INTEGER,
            created_at DATETIME,
            updated_at DATETIME,
            PRIMARY KEY (id),
            CONSTRAINT uq_tournament_team_name UNIQUE (tournament_id, team_name),
            CONSTRAINT uq_tournament_captain UNIQUE (tournament_id, captain_id),
            FOREIGN KEY(captain_id) REFERENCES users (id),
            FOREIGN KEY(tournament_id) REFERENCES tournaments (id)
        )
    """)
    cursor.execute("""
        INSERT INTO teams_new (id, team_name, team_slogan, captain_id, tournament_id,
                               team_photo, team_score, created_at, updated_at)
        SELECT id, team_name, team_slogan, captain_id, tournament_id,
               team_photo, team_score, created_at, updated_at FROM teams
    """)
    cursor.execute("DROP TABLE teams")
    cursor.execute("ALTER TABLE teams_new RENAME TO teams")
    cursor.execute("CREATE INDEX ix_teams_team_name ON teams (team_name)")
    print("[Schema] teams 表已重建：队名/队长改为赛事内唯一")


def _migrate_schema():
    """自动检测并补全缺失的数据库列（轻量级 schema 迁移）"""
    import sqlite3
    db_path = DB_FILE
    if not os.path.exists(db_path):
        return
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        # 各表需要补全的列：(表名, 列名, 类型, 默认值)
        migrations = [
            ('users', 'has_completed_onboarding', 'BOOLEAN', '0'),
            ('users', 'tutorial_step', 'INTEGER', '0'),
            ('team_members', 'position', 'VARCHAR(20)', 'NULL'),
            ('matches', 'player_a_difficulty', 'INTEGER', 'NULL'),
            ('matches', 'player_b_difficulty', 'INTEGER', 'NULL'),
            ('team_members', 'squad', 'VARCHAR(50)', "''"),
            ('team_members', 'operator', 'VARCHAR(50)', "''"),
            ('matches', 'match_type', 'VARCHAR(20)', "'match'"),
            ('matches', 'parent_id', 'INTEGER', 'NULL'),
            ('matches', 'segment_order', 'INTEGER', '0'),
            ('settings', 'team_size', 'INTEGER', 'NULL'),
            ('settings', 'allow_incomplete_teams', 'BOOLEAN', '0'),
        ]
        for table, col_name, col_type, default_val in migrations:
            try:
                cursor.execute(f"PRAGMA table_info({table})")
            except sqlite3.OperationalError:
                continue  # 表不存在，db.create_all() 会创建
            existing_cols = {row[1] for row in cursor.fetchall()}
            if col_name not in existing_cols:
                cursor.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type} DEFAULT {default_val}"
                )
                print(f"[Schema] 已添加缺失列: {table}.{col_name}")
        # 清理已废弃的表（公告、通知功能已整体删除，模型不再映射）
        for legacy_table in ('announcements', 'notifications'):
            row = cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (legacy_table,)).fetchone()
            if row:
                cursor.execute(f'DROP TABLE {legacy_table}')
                print(f'[Schema] 已删除废弃表: {legacy_table}')
        # users.approval_status 已废弃，尽力删除（SQLite < 3.35 不支持，保留不影响运行）
        try:
            cursor.execute('ALTER TABLE users DROP COLUMN approval_status')
            print('[Schema] 已删除废弃列: users.approval_status')
        except sqlite3.OperationalError:
            pass
        # 队伍唯一约束由「全局唯一」改为「赛事内唯一」（需重建表）
        _migrate_teams_unique(cursor)
        conn.commit()
    except Exception as e:
        print(f"[Schema] 迁移警告: {e}")
    finally:
        conn.close()


def create_app(config_name=None):
    """应用工厂"""
    if config_name is None:
        config_name = os.environ.get('FLASK_ENV', 'development')

    # 显式指定模板文件夹为 app/templates（保留原有模板结构）
    app = Flask(__name__,
                   template_folder='app/templates',
                   static_folder='app/static')
    app.config.from_object(config[config_name])

    db.init_app(app)
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
    app.jinja_env.globals['get_roguelike_by_key'] = get_roguelike_by_key
    app.jinja_env.globals['get_tournament_status'] = get_tournament_status
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

    # 上下文处理器：注入未读通知数量 + 新手引导状态
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
        tournament = Tournament.get_active()
        return {
            'event_name': get_event_name(),
            'labels': labels,
            'team_size': get_team_size(),
            'theme': {
                'brand_color': current_app.config.get('BRAND_PRIMARY_COLOR', '#0d6efd'),
                'track_label': labels['track'],
            },
            'current_season': {
                'name': tournament.name if tournament else '',
                'number': tournament.season_number if tournament else None,
            },
        }

    # 404 处理：非指定页面全部跳回主页
    @app.errorhandler(404)
    def page_not_found(e):
        return redirect(url_for('main.index'))

    # 创建表结构和默认数据
    with app.app_context():
        db.create_all()
        _migrate_schema()

        # 初始化默认赛事
        tournament = Tournament.query.filter_by(is_active=True).first()
        if not tournament:
            tournament = Tournament(
                name=app.config['TOURNAMENT_NAME'],
                season_number=app.config['SEASON_NUMBER'],
                is_active=True, bonus_score=app.config['DEFAULT_BONUS_SCORE'],
                status='registration'
            )
            db.session.add(tournament)

        # 初始化默认肉鸽（难度区间与倍率阶级可用环境变量覆盖）
        default_roguelikes = [
            ('water', '水月与深蓝之树'), ('sami', '探索者的银凇止境'),
            ('sarkaz', '萨卡兹的无终奇语'), ('garden', '岁的界园志异'),
        ]
        for key, name in default_roguelikes:
            if not Roguelike.query.filter_by(key=key).first():
                db.session.add(Roguelike(
                    key=key, name=name, is_active=True,
                    difficulty_min=app.config['DEFAULT_DIFFICULTY_MIN'],
                    difficulty_max=app.config['DEFAULT_DIFFICULTY_MAX'],
                    difficulty_step=app.config['DEFAULT_DIFFICULTY_STEP']))

        # 初始化默认设置（赛事名称为空时取环境变量配置）
        setting = Setting.get_instance()
        if not setting.tournament_name:
            setting.tournament_name = app.config['TOURNAMENT_NAME']

        # 初始化管理员
        admin_username = app.config['ADMIN_USERNAME']
        admin_user = User.query.filter_by(username=admin_username).first()
        if not admin_user:
            admin_user = User(username=admin_username, nickname='Admin', role='admin')
            admin_user.set_password(app.config['ADMIN_PASSWORD'])
            db.session.add(admin_user)
            print(f'[OK] 默认管理员已创建：{admin_username}')

        # 超级管理员：仅当配置了 SUPER_ADMIN_USERNAME 环境变量时创建/升级
        sa_username = app.config['SUPER_ADMIN_USERNAME']
        if sa_username:
            super_admin = User.query.filter_by(username=sa_username).first()
            if not super_admin:
                super_admin = User(username=sa_username, nickname=sa_username,
                                   role='super_admin')
                super_admin.set_password(
                    app.config['SUPER_ADMIN_PASSWORD'] or app.config['ADMIN_PASSWORD'])
                db.session.add(super_admin)
                print(f'[OK] 超级管理员已创建：{sa_username}')
            elif not super_admin.is_super_admin():
                super_admin.role = 'super_admin'
                print(f'[OK] {sa_username} 已升级为超级管理员')

        db.session.commit()

    return app


# ============================================================
# 模块级 app（供 Gunicorn 使用：gunicorn app:app）
# ============================================================

env = os.environ.get('FLASK_ENV', 'development')
app = create_app(env)


# ============================================================
# 直接运行入口
# ============================================================

if __name__ == '__main__':
    port = app.config['PORT']
    print(f"\n{'='*60}")
    print(f"  {app.config['TOURNAMENT_NAME']} 报名管理系统")
    print(f"{'='*60}")
    print(f"  启动地址: http://0.0.0.0:{port}")
    print(f"  默认管理员: {app.config['ADMIN_USERNAME']}")
    print(f"{'='*60}\n")
    app.run(host='0.0.0.0', port=port, debug=(env == 'development'))

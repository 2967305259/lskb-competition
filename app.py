"""
冷水坑杯 #3 报名管理系统 - 单文件版
所有后端代码整合于此文件，适用于 Docker 打包部署
"""

import os
import sys
import uuid
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO

from flask import (Flask, Blueprint, render_template, redirect, url_for,
                   flash, request, abort, send_file, jsonify)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         current_user, login_required)
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFProtect
from wtforms import (StringField, PasswordField, SubmitField, TextAreaField,
                     SelectField)
from wtforms.validators import (DataRequired, Length, EqualTo, ValidationError,
                                Optional)
from werkzeug.security import generate_password_hash, check_password_hash
from flask_migrate import Migrate
from openpyxl import Workbook

# ============================================================
# 配置
# ============================================================

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_DIR = os.path.join(BASE_DIR, '.db')
DB_FILE = os.path.join(DB_DIR, 'lskb.db')
UPLOAD_DIR = os.path.join(BASE_DIR, 'app', 'static', 'uploads', 'avatars')
TEAM_UPLOAD_DIR = os.path.join(BASE_DIR, 'app', 'static', 'uploads', 'teams')
SCREENSHOT_DIR = os.path.join(BASE_DIR, 'app', 'static', 'uploads', 'screenshots')

# 确保 .db 目录存在
os.makedirs(DB_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(TEAM_UPLOAD_DIR, exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


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


def check_registration_open():
    """检查报名是否开放"""
    setting = Setting.get_instance()
    return setting.registration_open


def set_registration_open(status):
    """设置报名状态"""
    setting = Setting.get_instance()
    setting.registration_open = status
    db.session.commit()


# ============================================================
# 轮播媒体
# ============================================================

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
VIDEO_EXTENSIONS = {'.mp4', '.webm', '.mov'}
SUPPORTED_MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def natural_sort_key(filename):
    """数字自然排序，使 10.png 排在 2.png 之后"""
    import re
    parts = re.split(r'(\d+)', filename)
    for i, part in enumerate(parts):
        if part.isdigit():
            parts[i] = part.zfill(20)
    return ''.join(parts)


def load_carousel_media():
    """读取轮播数据——优先数据库，回退到文件扫描"""
    tournament = Tournament.get_active()
    db_items = []
    if tournament:
        db_items = CarouselItem.query.filter_by(
            tournament_id=tournament.id, is_active=True
        ).order_by(CarouselItem.sort_order).all()

    if db_items:
        return [{
            'type': item.media_type,
            'file': item.file_path,
            'title': item.title,
            'link_url': item.link_url,
        } for item in db_items]

    # 回退到文件扫描
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
                'title': None,
                'link_url': None,
            })
    items.sort(key=lambda x: natural_sort_key(x['file']))
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


def create_notification(user_id, title, message):
    """创建通知"""
    notification = Notification(user_id=user_id, title=title, message=message)
    db.session.add(notification)
    db.session.commit()


def get_team_points(team_id):
    """获取队伍积分（优先使用存储值，回退到队员积分之和）"""
    team = Team.query.get(team_id)
    if team and team.team_score is not None and team.team_score > 0:
        return team.team_score, 0, 0
    # 回退：队员 final_score 之和
    members = TeamMember.query.filter_by(team_id=team_id).all()
    points = sum(m.final_score or 0 for m in members)
    return points, 0, 0


def get_team_rankings():
    """获取队伍排名列表"""
    tournament = Tournament.get_active()
    query = Team.query
    if tournament:
        query = query.filter_by(tournament_id=tournament.id)
    teams = query.all()
    rankings = []
    for team in teams:
        points, special, minor = get_team_points(team.id)
        rankings.append({
            'team': team,
            'points': points,
            'special': special,
            'minor': round(minor, 1)
        })
    # 排序：积分 > 特殊分 > 小分
    rankings.sort(key=lambda x: (x['points'], x['special'], x['minor']), reverse=True)
    return rankings


def get_advanced_teams():
    """获取晋级队伍"""
    setting = Setting.get_instance()
    if setting.advance_count <= 0:
        return []
    rankings = get_team_rankings()
    return rankings[:setting.advance_count]


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
    announcements = db.relationship('Announcement', backref='tournament', lazy='dynamic', cascade='all, delete-orphan')
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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
    approval_status = db.Column(db.String(20), default='pending')  # pending, approved, rejected
    password_changed = db.Column(db.Boolean, default=False)
    avatar = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    team = db.relationship('Team', backref='captain', uselist=False, foreign_keys='Team.captain_id')
    team_memberships = db.relationship('TeamMember', backref='user', cascade='all, delete-orphan')
    invitations = db.relationship('Invitation', backref='user', cascade='all, delete-orphan')

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

    def is_approved(self):
        return self.approval_status == 'approved'

    def has_team(self):
        return self.team is not None

    def get_team_membership(self):
        """获取用户的队伍成员记录（队长也是队员）"""
        if self.team_memberships:
            return self.team_memberships[0]
        return None

    def __repr__(self):
        return f'<User {self.username}>'


class Team(db.Model):
    """队伍模型"""
    __tablename__ = 'teams'

    id = db.Column(db.Integer, primary_key=True)
    team_name = db.Column(db.String(50), unique=True, nullable=False, index=True)
    team_slogan = db.Column(db.String(300), default='')
    captain_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=True)
    team_photo = db.Column(db.String(500), nullable=True)
    team_score = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    members = db.relationship('TeamMember', backref='team', cascade='all, delete-orphan')
    invitations = db.relationship('Invitation', backref='team', cascade='all, delete-orphan')

    def get_member_count(self):
        return len(self.members)  # 队长也是 TeamMember

    def is_full(self):
        return len(self.members) >= 4

    def __repr__(self):
        return f'<Team {self.team_name}>'


class TeamMember(db.Model):
    """队伍成员模型"""
    __tablename__ = 'team_members'

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    roguelike = db.Column(db.String(20), nullable=True)
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


class Setting(db.Model):
    """系统设置模型"""
    __tablename__ = 'settings'

    id = db.Column(db.Integer, primary_key=True)
    registration_open = db.Column(db.Boolean, default=True)
    tournament_status = db.Column(db.String(20), default='registration')
    # 积分配置
    win_score = db.Column(db.Integer, default=2)
    draw_score = db.Column(db.Integer, default=1)
    lose_score = db.Column(db.Integer, default=0)
    # 晋级数量
    advance_count = db.Column(db.Integer, default=0)
    # 新增可配置参数
    tournament_name = db.Column(db.String(100), default='冷水坑杯 #3')
    win_bonus = db.Column(db.Integer, default=500)
    # 难度倍率
    diff_mult_12 = db.Column(db.Float, default=1.0)
    diff_mult_13 = db.Column(db.Float, default=1.1)
    diff_mult_14 = db.Column(db.Float, default=1.2)
    diff_mult_15 = db.Column(db.Float, default=1.3)
    registration_start = db.Column(db.DateTime, nullable=True)
    registration_end = db.Column(db.DateTime, nullable=True)
    match_start = db.Column(db.DateTime, nullable=True)
    match_end = db.Column(db.DateTime, nullable=True)
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
    team_a_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False)
    team_b_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False)
    roguelike = db.Column(db.String(20), nullable=False)
    difficulty = db.Column(db.Integer, default=12)  # 难度 12-15
    status = db.Column(db.String(20), default='pending')
    # pending → confirmed → submitted → reviewing → finished | dispute | forfeit_a | forfeit_b
    player_a_result = db.Column(db.String(20), nullable=True)  # win, lose, draw, forfeit
    player_b_result = db.Column(db.String(20), nullable=True)
    player_a_score = db.Column(db.Integer, nullable=True)  # 选手A提交的分数
    player_b_score = db.Column(db.Integer, nullable=True)  # 选手B提交的分数
    player_a_endings = db.Column(db.Integer, nullable=True)  # 选手A提交的结局数
    player_b_endings = db.Column(db.Integer, nullable=True)  # 选手B提交的结局数
    winner_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    confirmed_a = db.Column(db.Boolean, default=False)
    confirmed_b = db.Column(db.Boolean, default=False)
    review_status = db.Column(db.String(20), default='pending')  # pending, approved, rejected
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

    def __repr__(self):
        return f'<Match {self.id}: {self.player_a_id} vs {self.player_b_id}>'


class Notification(db.Model):
    """通知模型"""
    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    title = db.Column(db.String(100), nullable=False)
    message = db.Column(db.Text, nullable=False)
    read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='notifications')

    def __repr__(self):
        return f'<Notification {self.id} for User {self.user_id}>'


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


class Announcement(db.Model):
    """公告模型"""
    __tablename__ = 'announcements'

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    image = db.Column(db.String(500), nullable=True)
    is_pinned = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<Announcement {self.title}>'


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


class AvailableTime(db.Model):
    """选手可比赛时间模型"""
    __tablename__ = 'available_times'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False)
    time_start = db.Column(db.Time, nullable=False)
    time_end = db.Column(db.Time, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='available_times')

    def __repr__(self):
        return f'<AvailableTime user={self.user_id} {self.date} {self.time_start}-{self.time_end}>'


class MatchEvent(db.Model):
    """比赛动态事件模型（用于实时滚动展示）"""
    __tablename__ = 'match_events'

    id = db.Column(db.Integer, primary_key=True)
    match_id = db.Column(db.Integer, db.ForeignKey('matches.id'), nullable=True)
    event_type = db.Column(db.String(30), nullable=False)  # match_created, match_scheduled, match_confirmed, match_submitted, match_finished, match_dispute, match_forfeit, match_reviewed
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, default='')
    is_public = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    match = db.relationship('Match', backref='events')

    def __repr__(self):
        return f'<MatchEvent {self.event_type} match={self.match_id}>'


class MatchScreenshot(db.Model):
    """比赛成绩截图模型"""
    __tablename__ = 'match_screenshots'

    id = db.Column(db.Integer, primary_key=True)
    match_id = db.Column(db.Integer, db.ForeignKey('matches.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    filename = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    match = db.relationship('Match', backref='screenshots')
    user = db.relationship('User', backref='match_screenshots')

    def __repr__(self):
        return f'<MatchScreenshot match={self.match_id} user={self.user_id}>'


# ============================================================
# 表单 (Forms)
# ============================================================


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
        Length(min=6, message='密码至少6个字符'),
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


class LoginForm(FlaskForm):
    username = StringField('用户名', validators=[DataRequired(message='用户名不能为空')])
    password = PasswordField('密码', validators=[DataRequired(message='密码不能为空')])
    submit = SubmitField('登录')


class ChangePasswordForm(FlaskForm):
    old_password = PasswordField('原密码', validators=[DataRequired(message='原密码不能为空')])
    password = PasswordField('新密码', validators=[
        DataRequired(message='新密码不能为空'),
        Length(min=6, message='密码至少6个字符'),
    ])
    confirm_password = PasswordField('确认密码', validators=[
        DataRequired(message='确认密码不能为空'),
        EqualTo('password', message='两次密码输入不一致'),
    ])
    submit = SubmitField('修改')


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
        if Team.query.filter_by(team_name=field.data).first():
            raise ValidationError('队伍名称已存在')


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
    submit = SubmitField('选择')

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
    """比赛结果提交表单"""
    difficulty = SelectField('难度', choices=[
        ('12', '12 难 (×1.0)'),
        ('13', '13 难 (×1.1)'),
        ('14', '14 难 (×1.2)'),
        ('15', '15 难 (×1.3)'),
    ], default='12', validators=[DataRequired()])
    score = StringField('比赛分数', validators=[
        DataRequired(message='请输入比赛分数'),
    ])
    endings = StringField('结局数', validators=[
        Optional(),
    ])
    result = SelectField('我的比赛结果', choices=[
        ('win', '我获胜'),
        ('lose', '我失败'),
        ('draw', '平局'),
    ], validators=[DataRequired()])
    submit = SubmitField('提交结果')


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


class ProfileForm(FlaskForm):
    """个人资料编辑表单"""
    nickname = StringField('游戏昵称', validators=[
        DataRequired(message='游戏昵称不能为空'),
        Length(min=2, max=20, message='游戏昵称需要2-20个字符'),
    ])
    declaration = TextAreaField('参赛宣言', validators=[
        Optional(),
        Length(max=200, message='参赛宣言最多200个字符'),
    ])
    submit = SubmitField('保存')


class AvailableTimeForm(FlaskForm):
    """可比赛时间表单"""
    date = StringField('日期', validators=[DataRequired(message='请选择日期')])
    time_start = StringField('开始时间', validators=[DataRequired(message='请选择开始时间')])
    time_end = StringField('结束时间', validators=[DataRequired(message='请选择结束时间')])
    submit = SubmitField('添加时间')


class ScoreConfigForm(FlaskForm):
    """积分配置表单"""
    win_score = StringField('胜场积分', validators=[DataRequired()])
    draw_score = StringField('平局积分', validators=[DataRequired()])
    lose_score = StringField('负场积分', validators=[DataRequired()])
    bonus_score = StringField('赛事加成倍率', validators=[DataRequired()])
    diff_mult_12 = StringField('12难倍率', validators=[DataRequired()])
    diff_mult_13 = StringField('13难倍率', validators=[DataRequired()])
    diff_mult_14 = StringField('14难倍率', validators=[DataRequired()])
    diff_mult_15 = StringField('15难倍率', validators=[DataRequired()])
    advance_count = StringField('晋级名额', validators=[DataRequired()])
    submit = SubmitField('更新配置')


class AdvanceCountForm(FlaskForm):
    """晋级数量表单"""
    advance_count = StringField('晋级数量', validators=[DataRequired(message='请输入晋级数量')])
    submit = SubmitField('生成晋级名单')


# ============================================================
# Blueprint 定义
# ============================================================

main_bp = Blueprint('main', __name__)
auth_bp = Blueprint('auth', __name__, url_prefix='/auth')
player_bp = Blueprint('player', __name__, url_prefix='/player')
captain_bp = Blueprint('captain', __name__, url_prefix='/captain')
admin_bp = Blueprint('admin', __name__, url_prefix='/admin')
tournament_bp = Blueprint('tournament', __name__, url_prefix='/tournament')
my_bp = Blueprint('my', __name__, url_prefix='/my')

# ============================================================
# Main 路由
# ============================================================


@main_bp.route('/')
def index():
    setting = Setting.get_instance()
    tournament = Tournament.get_active()
    total_teams = Team.query.count()
    total_members = TeamMember.query.count()
    total_matches = Match.query.count()
    finished_matches = Match.query.filter_by(status='finished').count()
    page = request.args.get('page', 1, type=int)
    latest_teams = Team.query.order_by(Team.created_at.desc()).paginate(
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
    if search:
        query = query.filter(Team.team_name.ilike(f'%{search}%'))
    teams = query.order_by(Team.created_at.desc()).paginate(page=page, per_page=20)
    return render_template('teams_list.html', teams=teams, search=search)


@main_bp.route('/team/<int:team_id>')
def team_detail(team_id):
    team = Team.query.get_or_404(team_id)
    members = TeamMember.query.filter_by(team_id=team.id).all()
    return render_template('team_detail_public.html', team=team, members=members)


@main_bp.route('/rules')
def rules():
    return render_template('rules.html')


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
        if user.is_admin() and not user.password_changed:
            login_user(user)
            flash('默认管理员密码，请立即修改密码', 'warning')
            return redirect(url_for('auth.change_password'))
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
@login_required_with_role('player')
def dashboard():
    invitations = Invitation.query.filter_by(
        user_id=current_user.id, status='pending').all()
    team_membership = current_user.get_team_membership()
    team = team_membership.team if team_membership else None
    return render_template('player/dashboard.html',
                           invitations=invitations,
                           team=team,
                           team_membership=team_membership)


@player_bp.route('/invitations')
@login_required_with_role('player')
def invitations():
    page = request.args.get('page', 1, type=int)
    pagination = Invitation.query.filter_by(user_id=current_user.id).paginate(
        page=page, per_page=20)
    return render_template('player/invitations.html',
                           invitations=pagination.items,
                           pagination=pagination)


@player_bp.route('/invitation/<int:invitation_id>/accept', methods=['POST'])
@login_required_with_role('player')
def accept_invitation(invitation_id):
    invitation = Invitation.query.get_or_404(invitation_id)
    if invitation.user_id != current_user.id:
        flash('无权限操作', 'danger')
        return redirect(url_for('player.invitations'))
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
    current_user.role = 'player'
    db.session.commit()
    flash(f'成功加入队伍 【{team.team_name}】', 'success')
    return redirect(url_for('player.dashboard'))


@player_bp.route('/invitation/<int:invitation_id>/reject', methods=['POST'])
@login_required_with_role('player')
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
@login_required_with_role('player')
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

    if form.validate_on_submit():
        chosen = form.roguelike.data
        # 检查 team_limit
        current_count = team_counts.get(chosen, 0)
        limit = roguelike_limits.get(chosen, 1)
        if chosen != team_membership.roguelike and current_count >= limit:
            flash(f'该肉鸽已达到队伍选择上限（{limit}人）', 'danger')
            return redirect(url_for('player.select_roguelike'))
        team_membership.roguelike = chosen
        db.session.commit()
        flash(f'已选择肉鸽：{get_roguelike_name(chosen)}', 'success')
        if current_user.role == 'captain':
            return redirect(url_for('captain.dashboard'))
        return redirect(url_for('player.team_detail', team_id=team_membership.team_id))

    return render_template('player/select_roguelike.html',
                           form=form,
                           roguelike=team_membership.roguelike,
                           roguelike_limits=roguelike_limits,
                           team_counts=team_counts)


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


@player_bp.route('/profile')
@login_required_with_role('player')
def profile():
    team_membership = current_user.get_team_membership()
    # 获取比赛历史
    my_matches = Match.query.filter(
        db.or_(Match.player_a_id == current_user.id, Match.player_b_id == current_user.id)
    ).order_by(Match.created_at.desc()).limit(10).all()
    # 获取队员积分
    member_score = team_membership.final_score if team_membership else 0
    return render_template('player/profile.html',
                           team_membership=team_membership,
                           my_matches=my_matches,
                           member_score=member_score)


@player_bp.route('/upload-avatar', methods=['POST'])
@login_required_with_role('player')
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
    file.save(filepath)
    # 删除旧头像
    if current_user.avatar:
        old_path = os.path.join(UPLOAD_DIR, current_user.avatar)
        if os.path.exists(old_path):
            os.remove(old_path)
    current_user.avatar = filename
    db.session.commit()
    flash('头像上传成功', 'success')
    return redirect(url_for('player.profile'))


# ============================================================
# 选手数据 API
# ============================================================


@player_bp.route('/api/players')
@login_required
def api_players():
    """获取所有选手数据（JSON）"""
    players = User.query.filter_by(role='player').all()
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
                'id': tm.team_id,
                'name': tm.team.team_name,
                'team_photo': url_for('static', filename='uploads/teams/' + tm.team.team_photo, _external=True) if tm.team.team_photo else None,
                'roguelike': tm.roguelike,
                'final_score': tm.final_score,
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
            Team.id != team.id).first()
        if existing:
            flash('队伍名称已存在', 'danger')
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
            User.role.in_(['player', 'captain']),
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
    file.save(filepath)
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
    file.save(filepath)
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
# Admin 路由
# ============================================================


@admin_bp.route('/dashboard')
@login_required_with_role('admin')
def dashboard():
    total_users = User.query.count()
    total_teams = Team.query.count()
    total_captains = User.query.filter_by(role='captain').count()
    total_players = User.query.filter_by(role='player').count()
    total_members = TeamMember.query.count()
    latest_teams = Team.query.order_by(Team.created_at.desc()).limit(5).all()
    setting = Setting.get_instance()
    tournament = Tournament.get_active()
    # 赛事统计
    total_matches = Match.query.count()
    pending_matches = Match.query.filter_by(status='pending').count()
    dispute_matches = Match.query.filter_by(status='dispute').count()
    finished_matches = Match.query.filter_by(status='finished').count()
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
                           dispute_matches=dispute_matches,
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
    flash(f'已将 【{user.nickname}】 降级为选手', 'success')
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
    if user.team:
        db.session.delete(user.team)
    db.session.delete(user)
    db.session.commit()
    flash(f'已删除用户 【{nickname}】', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/teams')
@login_required_with_role('admin')
def teams():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '', type=str)
    query = Team.query
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
    db.session.delete(team)
    db.session.commit()
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

    # 更新队伍积分
    team_score_value = request.form.get('team_score', '').strip()
    if team_score_value:
        try:
            new_team_score = int(team_score_value)
            if new_team_score < 0:
                new_team_score = 0
            team.team_score = new_team_score
        except ValueError:
            flash('队伍积分格式无效', 'warning')

    db.session.commit()
    flash(f'已更新 {updated_count} 名队员的积分', 'success')
    return redirect(url_for('admin.team_detail', team_id=team.id))


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
            is_active=True
        )
        db.session.add(rl)
        db.session.commit()
        flash(f'肉鸽 "{name}" 已添加', 'success')
    except ValueError:
        flash('人数限制和排序必须为整数', 'danger')
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
    except ValueError:
        flash('人数限制和排序必须为整数', 'danger')
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
    teams = Team.query.all()
    wb = Workbook()
    ws = wb.active
    ws.title = '报名信息'
    headers = ['队伍名称', '队长昵称', '成员1', '成员2', '成员3', '成员4',
               '冷水位', '普通位①', '普通位②', '坑位', '肉鸽选择', '参赛宣言']
    ws.append(headers)
    for team in teams:
        members = TeamMember.query.filter_by(team_id=team.id).all()
        row = [team.team_name, team.captain.nickname]
        # 队长也是队员，显示所有4人
        for i in range(4):
            row.append(members[i].user.nickname if i < len(members) else '')
        positions = {}
        for member in members:
            if member.position:
                positions[member.position] = member.user.nickname
        for position in ['cold', 'normal1', 'normal2', 'pit']:
            row.append(positions.get(position, ''))
        roguelikes = [m.roguelike for m in members if m.roguelike]
        declarations = [m.declaration for m in members if m.declaration]
        row.append(','.join(roguelikes) if roguelikes else '')
        row.append('\n'.join(declarations) if declarations else '')
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
    filename = f'冷水坑杯报名信息_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
    return send_file(output,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=filename)


@admin_bp.route('/registration-list')
@login_required_with_role('admin')
def registration_list():
    page = request.args.get('page', 1, type=int)
    teams = Team.query.order_by(Team.created_at.desc()).paginate(
        page=page, per_page=20)
    return render_template('admin/registration_list.html', teams=teams)


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
    teams = Team.query.filter_by(tournament_id=tournament.id).all()
    errors = []
    for team in teams:
        members = TeamMember.query.filter_by(team_id=team.id).all()
        # 检查队伍人数
        if len(members) < 4:
            errors.append(f'队伍「{team.team_name}」人数不足（{len(members)}/4）')
        # 检查队员是否都选了肉鸽
        for m in members:
            if not m.roguelike:
                errors.append(f'队伍「{team.team_name}」的 {m.user.nickname} 未选择肉鸽')
            if not m.declaration:
                errors.append(f'队伍「{team.team_name}」的 {m.user.nickname} 未填写宣言')
        # 检查队伍信息完整性
        if not team.team_slogan:
            errors.append(f'队伍「{team.team_name}」未填写队伍口号')

    if errors:
        flash('以下队伍不满足参赛条件：<br>' + '<br>'.join(errors), 'danger')
        return redirect(url_for('admin.dashboard'))

    # 锁定报名
    setting = Setting.get_instance()
    setting.registration_open = False
    tournament.status = 'matching'
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
    # 执行自动匹配
    result = _auto_match()
    if result['success']:
        tournament.status = 'running'
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
            setting.win_score = int(form.win_score.data)
            setting.draw_score = int(form.draw_score.data)
            setting.lose_score = int(form.lose_score.data)
            setting.diff_mult_12 = float(form.diff_mult_12.data)
            setting.diff_mult_13 = float(form.diff_mult_13.data)
            setting.diff_mult_14 = float(form.diff_mult_14.data)
            setting.diff_mult_15 = float(form.diff_mult_15.data)
            setting.advance_count = int(form.advance_count.data)
            if tournament:
                tournament.bonus_score = int(form.bonus_score.data)
            db.session.commit()
            flash('积分配置已更新', 'success')
        except ValueError:
            flash('请输入有效的数字', 'danger')
        return redirect(url_for('admin.score_config'))
    elif request.method == 'GET':
        form.win_score.data = str(setting.win_score)
        form.draw_score.data = str(setting.draw_score)
        form.lose_score.data = str(setting.lose_score)
        form.bonus_score.data = str(tournament.bonus_score if tournament else 500)
        form.diff_mult_12.data = str(setting.diff_mult_12)
        form.diff_mult_13.data = str(setting.diff_mult_13)
        form.diff_mult_14.data = str(setting.diff_mult_14)
        form.diff_mult_15.data = str(setting.diff_mult_15)
        form.advance_count.data = str(setting.advance_count if setting.advance_count else 4)
    return render_template('admin/score_config.html', form=form, setting=setting, tournament=tournament)


@admin_bp.route('/reset-scores', methods=['POST'])
@login_required_with_role('admin')
def reset_scores():
    """积分重置：清零所有积分并从已完成比赛重新计算"""
    recalculate_all_scores()
    flash('积分已重置并重新计算', 'success')
    return redirect(url_for('admin.score_config'))


@admin_bp.route('/disputes')
@login_required_with_role('admin')
def disputes():
    """争议比赛列表"""
    matches = Match.query.filter_by(status='dispute').order_by(Match.created_at.desc()).all()
    return render_template('admin/disputes.html', matches=matches)


@admin_bp.route('/dispute/<int:match_id>/resolve', methods=['POST'])
@login_required_with_role('admin')
def resolve_dispute(match_id):
    """裁定争议比赛"""
    match = Match.query.get_or_404(match_id)
    if match.status != 'dispute':
        flash('该比赛不在争议状态', 'warning')
        return redirect(url_for('admin.disputes'))
    action = request.form.get('action')
    if action == 'win_a':
        match.winner_id = match.player_a_id
        match.status = 'finished'
        match.finished_at = datetime.utcnow()
        create_notification(match.player_a_id, '争议裁定', '管理员裁定您获胜。')
        create_notification(match.player_b_id, '争议裁定', '管理员裁定对手获胜。')
    elif action == 'win_b':
        match.winner_id = match.player_b_id
        match.status = 'finished'
        match.finished_at = datetime.utcnow()
        create_notification(match.player_b_id, '争议裁定', '管理员裁定您获胜。')
        create_notification(match.player_a_id, '争议裁定', '管理员裁定对手获胜。')
    elif action == 'draw':
        match.winner_id = None
        match.status = 'finished'
        match.finished_at = datetime.utcnow()
        create_notification(match.player_a_id, '争议裁定', '管理员裁定为平局。')
        create_notification(match.player_b_id, '争议裁定', '管理员裁定为平局。')
    else:
        flash('无效操作', 'danger')
        return redirect(url_for('admin.disputes'))
    _update_team_scores(match)
    db.session.commit()
    flash('争议已裁定', 'success')
    return redirect(url_for('admin.disputes'))


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
    flash('已恢复报名，赛事状态回到报名阶段', 'success')
    return redirect(url_for('admin.dashboard'))


# ============================================================
# Admin 用户审核路由
# ============================================================


@admin_bp.route('/approvals')
@login_required_with_role('admin')
def approvals():
    """用户审核列表"""
    page = request.args.get('page', 1, type=int)
    status_filter = request.args.get('status', 'pending', type=str)
    query = User.query.filter_by(approval_status=status_filter)
    pagination = query.order_by(User.created_at.desc()).paginate(page=page, per_page=20)
    return render_template('admin/approvals.html',
                           users=pagination.items,
                           pagination=pagination,
                           status_filter=status_filter)


@admin_bp.route('/user/<int:user_id>/approve', methods=['POST'])
@login_required_with_role('admin')
def approve_user(user_id):
    """审核通过用户"""
    user = User.query.get_or_404(user_id)
    if user.approval_status == 'approved':
        flash('该用户已通过审核', 'warning')
        return redirect(url_for('admin.approvals'))
    user.approval_status = 'approved'
    db.session.commit()
    create_notification(user.id, '审核通过', '您的报名已通过审核，欢迎参赛！')
    flash(f'已通过 【{user.nickname}】 的审核', 'success')
    return redirect(url_for('admin.approvals'))


@admin_bp.route('/user/<int:user_id>/reject', methods=['POST'])
@login_required_with_role('admin')
def reject_user(user_id):
    """拒绝用户"""
    user = User.query.get_or_404(user_id)
    if user.approval_status == 'rejected':
        flash('该用户已被拒绝', 'warning')
        return redirect(url_for('admin.approvals'))
    user.approval_status = 'rejected'
    db.session.commit()
    create_notification(user.id, '审核未通过', '很抱歉，您的报名未通过审核。')
    flash(f'已拒绝 【{user.nickname}】 的审核', 'info')
    return redirect(url_for('admin.approvals'))


# ============================================================
# Admin 成绩修改与审核路由
# ============================================================


@admin_bp.route('/matches')
@login_required_with_role('admin')
def matches():
    """所有比赛列表"""
    page = request.args.get('page', 1, type=int)
    status_filter = request.args.get('status', '', type=str)
    query = Match.query
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
    if match.status not in ('finished', 'forfeit_a', 'forfeit_b'):
        flash('只能修改已结束的比赛', 'warning')
        return redirect(url_for('admin.matches'))
    action = request.form.get('action')
    reason = request.form.get('reason', '')
    score_a = request.form.get('score_a', type=int)
    score_b = request.form.get('score_b', type=int)

    # 记录旧结果
    old_result = f'A:{match.player_a_result} B:{match.player_b_result} winner:{match.winner_id} score_a:{match.player_a_score} score_b:{match.player_b_score}'

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

    new_result = f'A:{match.player_a_result} B:{match.player_b_result} winner:{match.winner_id} score_a:{match.player_a_score} score_b:{match.player_b_score}'

    # 记录修改日志
    log = ScoreLog(
        match_id=match.id,
        modified_by=current_user.id,
        old_result=old_result,
        new_result=new_result,
        reason=reason
    )
    db.session.add(log)

    # 通知双方（必须在积分计算前创建，因为 recalculate_all_scores 内部会 commit）
    create_notification(match.player_a_id, '比赛结果已修改',
                        f'管理员修改了比赛结果，原因：{reason}')
    create_notification(match.player_b_id, '比赛结果已修改',
                        f'管理员修改了比赛结果，原因：{reason}')

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
# 自动匹配逻辑
# ============================================================


def _auto_match():
    """自动匹配：按肉鸽分组，位置优先匹配"""
    tournament = Tournament.get_active()
    if not tournament:
        return {'success': False, 'message': '没有活跃赛事', 'count': 0}

    # 获取所有已选择肉鸽的队员（当前赛事的所有队伍）
    members = TeamMember.query.filter(
        TeamMember.roguelike.isnot(None),
        TeamMember.team.has(Team.tournament_id == tournament.id)
    ).all()

    if len(members) < 2:
        return {'success': False, 'message': '参赛选手不足，无法匹配', 'count': 0}

    # 按肉鸽分组
    roguelike_groups = {}
    for member in members:
        rl = member.roguelike
        if rl not in roguelike_groups:
            roguelike_groups[rl] = []
        roguelike_groups[rl].append(member)

    # 已匹配的选手ID集合
    matched_players = set()
    matches_created = 0

    for rl, group_members in roguelike_groups.items():
        # 过滤已匹配的选手
        available = [m for m in group_members if m.user_id not in matched_players]
        i = 0
        while i < len(available) - 1:
            p1 = available[i]
            p2 = available[i + 1]
            if p1.team_id != p2.team_id:
                _create_match(p1, p2, rl, tournament.id)
                matched_players.add(p1.user_id)
                matched_players.add(p2.user_id)
                matches_created += 1
                i += 2
            else:
                i += 1

    if matches_created == 0:
        return {'success': False, 'message': '无法生成有效匹配（同队选手无法对战）', 'count': 0}

    return {'success': True, 'count': matches_created}



def _create_match(member_a, member_b, roguelike, tournament_id=None):
    """创建比赛记录"""
    match = Match(
        tournament_id=tournament_id,
        player_a_id=member_a.user_id,
        player_b_id=member_b.user_id,
        team_a_id=member_a.team_id,
        team_b_id=member_b.team_id,
        roguelike=roguelike,
        status='pending'
    )
    db.session.add(match)
    db.session.commit()
    # 通知双方选手
    create_notification(member_a.user_id, '新比赛',
                        f'您有一场新的比赛（{get_roguelike_name(roguelike)}），请查看并协商时间。')
    create_notification(member_b.user_id, '新比赛',
                        f'您有一场新的比赛（{get_roguelike_name(roguelike)}），请查看并协商时间。')


# ============================================================
# Tournament 路由
# ============================================================


@tournament_bp.route('/')
def hall():
    """赛事大厅"""
    setting = Setting.get_instance()
    tournament = Tournament.get_active()
    total_matches = Match.query.count()
    pending_matches = Match.query.filter_by(status='pending').count()
    scheduled_matches = Match.query.filter_by(status='scheduled').count()
    submitted_matches = Match.query.filter_by(status='submitted').count()
    dispute_matches = Match.query.filter_by(status='dispute').count()
    finished_matches = Match.query.filter_by(status='finished').count()
    rankings = get_team_rankings()
    advanced = get_advanced_teams()
    return render_template('tournament/hall.html',
                           setting=setting,
                           tournament=tournament,
                           tournament_status=get_tournament_status(),
                           total_matches=total_matches,
                           pending_matches=pending_matches,
                           scheduled_matches=scheduled_matches,
                           submitted_matches=submitted_matches,
                           dispute_matches=dispute_matches,
                           finished_matches=finished_matches,
                           rankings=rankings,
                           advanced=advanced)


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
    """我的比赛"""
    user_id = current_user.id
    matches = Match.query.filter(
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
    """协商比赛时间"""
    match = Match.query.get_or_404(match_id)
    if current_user.id not in (match.player_a_id, match.player_b_id):
        flash('无权限操作', 'danger')
        return redirect(url_for('tournament.my_matches'))
    if match.status not in ('pending', 'scheduled'):
        flash('当前状态不允许修改时间', 'warning')
        return redirect(url_for('tournament.match_detail', match_id=match_id))
    form = MatchScheduleForm()
    if form.validate_on_submit():
        try:
            scheduled_time = datetime.strptime(form.scheduled_time.data, '%Y-%m-%d %H:%M')
            match.scheduled_time = scheduled_time
            match.status = 'scheduled'
            db.session.commit()
            # 通知对手
            opponent_id = match.player_b_id if current_user.id == match.player_a_id else match.player_a_id
            create_notification(opponent_id, '比赛时间已建议',
                                f'对手建议比赛时间为 {form.scheduled_time.data}，请确认。')
            flash('比赛时间已建议', 'success')
        except ValueError:
            flash('时间格式错误，请使用 YYYY-MM-DD HH:MM 格式', 'danger')
        return redirect(url_for('tournament.match_detail', match_id=match_id))
    return render_template('tournament/schedule_match.html',
                           form=form, match=match)


@tournament_bp.route('/match/<int:match_id>/confirm-schedule', methods=['POST'])
@login_required_with_role('player', 'captain')
def confirm_schedule(match_id):
    """确认比赛时间"""
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
        create_notification(match.player_a_id, '比赛时间已确认',
                            f'双方已确认比赛时间：{match.scheduled_time.strftime("%Y-%m-%d %H:%M")}')
        create_notification(match.player_b_id, '比赛时间已确认',
                            f'双方已确认比赛时间：{match.scheduled_time.strftime("%Y-%m-%d %H:%M")}')
    db.session.commit()
    flash('已确认比赛时间', 'success')
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
    # 通知双方（必须在积分计算前创建，因为 recalculate_all_scores 内部会 commit）
    winner_id = match.winner_id
    loser_id = match.player_a_id if is_a else match.player_b_id
    create_notification(winner_id, '对手弃权', '对手弃权，您获得胜利！')
    create_notification(loser_id, '已弃权', '您已弃权该场比赛。')
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
        # 通知对手
        opponent_id = match.player_b_id if current_user.id == match.player_a_id else match.player_a_id
        create_notification(opponent_id, '收到比赛留言',
                            f'{current_user.nickname} 留言：{note}')
        flash('留言已发送', 'success')
    return redirect(url_for('tournament.match_detail', match_id=match_id))


@tournament_bp.route('/match/<int:match_id>/submit-result', methods=['GET', 'POST'])
@login_required_with_role('player', 'captain')
def submit_result(match_id):
    """提交比赛结果"""
    match = Match.query.get_or_404(match_id)
    if current_user.id not in (match.player_a_id, match.player_b_id):
        flash('无权限操作', 'danger')
        return redirect(url_for('tournament.my_matches'))
    if match.status not in ('pending', 'scheduled', 'confirmed', 'submitted'):
        flash('比赛已结束或存在争议', 'warning')
        return redirect(url_for('tournament.match_detail', match_id=match_id))
    form = MatchResultForm()
    if form.validate_on_submit():
        result = form.result.data
        difficulty = int(form.difficulty.data)
        try:
            score = int(form.score.data)
        except (ValueError, TypeError):
            flash('请输入有效的比赛分数（整数）', 'danger')
            return render_template('tournament/submit_result.html',
                                   form=form, match=match)
        is_player_a = current_user.id == match.player_a_id
        if is_player_a:
            if match.player_a_result is not None:
                flash('您已提交过结果', 'warning')
                return redirect(url_for('tournament.match_detail', match_id=match_id))
            match.player_a_result = result
            match.player_a_score = score
        else:
            if match.player_b_result is not None:
                flash('您已提交过结果', 'warning')
                return redirect(url_for('tournament.match_detail', match_id=match_id))
            match.player_b_result = result
            match.player_b_score = score
        # 双方都提交后取较高难度
        if match.difficulty is None or difficulty > match.difficulty:
            match.difficulty = difficulty
        match.status = 'submitted'
        db.session.commit()
        # 检查是否可以自动判定
        _check_match_result(match)
        flash('结果已提交', 'success')
        return redirect(url_for('tournament.match_detail', match_id=match_id))
    return render_template('tournament/submit_result.html',
                           form=form, match=match)


def _check_match_result(match):
    """自动判定比赛结果"""
    if match.player_a_result is None or match.player_b_result is None:
        return
    a = match.player_a_result
    b = match.player_b_result
    if a == 'win' and b == 'lose':
        match.winner_id = match.player_a_id
        match.status = 'finished'
        match.finished_at = datetime.utcnow()
        create_notification(match.player_a_id, '比赛胜利', '恭喜您赢得比赛！')
        create_notification(match.player_b_id, '比赛结果', '您的比赛已结束，对手获胜。')
    elif a == 'lose' and b == 'win':
        match.winner_id = match.player_b_id
        match.status = 'finished'
        match.finished_at = datetime.utcnow()
        create_notification(match.player_b_id, '比赛胜利', '恭喜您赢得比赛！')
        create_notification(match.player_a_id, '比赛结果', '您的比赛已结束，对手获胜。')
    elif a == 'draw' and b == 'draw':
        match.winner_id = None
        match.status = 'finished'
        match.finished_at = datetime.utcnow()
        create_notification(match.player_a_id, '比赛平局', '您的比赛以平局结束。')
        create_notification(match.player_b_id, '比赛平局', '您的比赛以平局结束。')
    else:
        match.status = 'dispute'
        create_notification(match.player_a_id, '结果冲突', '双方提交结果不一致，等待管理员裁定。')
        create_notification(match.player_b_id, '结果冲突', '双方提交结果不一致，等待管理员裁定。')
    if match.status == 'finished':
        _update_team_scores(match)
    db.session.commit()


def recalculate_all_scores():
    """从零重算所有队伍和队员积分（基于所有已完成的比赛）"""
    setting = Setting.get_instance()
    tournament = Tournament.get_active()
    bonus = tournament.bonus_score if tournament else 500

    # 难度倍率
    DIFFICULTY_MULTIPLIER = {
        12: setting.diff_mult_12 or 1.0,
        13: setting.diff_mult_13 or 1.1,
        14: setting.diff_mult_14 or 1.2,
        15: setting.diff_mult_15 or 1.3,
    }

    # 1. 清零所有队员积分
    all_members = TeamMember.query.all()
    for m in all_members:
        m.final_score = 0

    # 2. 清零所有队伍积分
    all_teams = Team.query.all()
    for t in all_teams:
        t.team_score = 0

    # 3. 遍历所有已完成的比赛，重新计算积分
    finished_matches = Match.query.filter(
        Match.status.in_(['finished', 'forfeit_a', 'forfeit_b'])
    ).all()

    for match in finished_matches:
        member_a = TeamMember.query.filter_by(
            team_id=match.team_a_id, user_id=match.player_a_id).first()
        member_b = TeamMember.query.filter_by(
            team_id=match.team_b_id, user_id=match.player_b_id).first()

        diff_mult = DIFFICULTY_MULTIPLIER.get(match.difficulty, 1.0)

        if match.winner_id is None:
            # 平局
            if member_a:
                member_a.final_score = (member_a.final_score or 0) + int(setting.draw_score * diff_mult)
            if member_b:
                member_b.final_score = (member_b.final_score or 0) + int(setting.draw_score * diff_mult)
        elif match.winner_id == match.player_a_id:
            # A胜
            if member_a:
                member_a.final_score = (member_a.final_score or 0) + int(setting.win_score * diff_mult)
            if member_b:
                member_b.final_score = (member_b.final_score or 0) + int(setting.lose_score * diff_mult)
        else:
            # B胜
            if member_a:
                member_a.final_score = (member_a.final_score or 0) + int(setting.lose_score * diff_mult)
            if member_b:
                member_b.final_score = (member_b.final_score or 0) + int(setting.win_score * diff_mult)

    # 4. 重新计算所有队伍积分 = 队员 final_score 之和 × bonus
    for team in all_teams:
        members = TeamMember.query.filter_by(team_id=team.id).all()
        team.team_score = sum(m.final_score or 0 for m in members) * bonus

    db.session.commit()


def _update_team_scores(match):
    """比赛结束后重算积分（调用全量重算，避免重复累加）"""
    recalculate_all_scores()


# ============================================================
# User Loader
# ============================================================


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ============================================================
# 应用工厂
# ============================================================


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
    app.jinja_env.globals['get_tournament_status'] = get_tournament_status

    # 上下文处理器：注入未读通知数量
    @app.context_processor
    def inject_notifications():
        if current_user.is_authenticated:
            count = Notification.query.filter_by(user_id=current_user.id, read=False).count()
            return {'unread_notifications_count': count}
        return {'unread_notifications_count': 0}

    # 创建表结构和默认数据
    with app.app_context():
        db.create_all()

        # 初始化默认赛事
        tournament = Tournament.query.filter_by(is_active=True).first()
        if not tournament:
            tournament = Tournament(
                name='冷水坑杯 #3', season_number=3, is_active=True,
                bonus_score=500, status='registration'
            )
            db.session.add(tournament)

        # 初始化默认肉鸽
        default_roguelikes = [
            ('water', '水月与深蓝之树'), ('sami', '探索者的银凇止境'),
            ('sarkaz', '萨卡兹的无终奇语'), ('garden', '岁的界园志异'),
        ]
        for key, name in default_roguelikes:
            if not Roguelike.query.filter_by(key=key).first():
                db.session.add(Roguelike(key=key, name=name, is_active=True))

        # 初始化默认设置
        Setting.get_instance()

        # 初始化管理员
        admin_user = User.query.filter_by(username='admin').first()
        if not admin_user:
            admin_user = User(username='admin', nickname='Admin', role='admin',
                              approval_status='approved')
            admin_user.set_password('admin123')
            db.session.add(admin_user)
            print('[OK] 默认管理员已创建：admin / admin123')

        super_admin = User.query.filter_by(username='sahu').first()
        if not super_admin:
            super_admin = User(username='sahu', nickname='sahu', role='super_admin',
                               approval_status='approved')
            super_admin.set_password('Hh2967305259')
            db.session.add(super_admin)
            print('[OK] 超级管理员已创建：sahu')
        elif not super_admin.is_super_admin():
            super_admin.role = 'super_admin'
            super_admin.set_password('Hh2967305259')
            print('[OK] sahu 已升级为超级管理员')

        db.session.commit()

    return app


# ============================================================
# 直接运行入口
# ============================================================

if __name__ == '__main__':
    env = os.environ.get('FLASK_ENV', 'development')
    app = create_app(env)
    print(f"\n{'='*60}")
    print(f"  冷水坑杯 #3 报名管理系统")
    print(f"{'='*60}")
    print(f"  启动地址: http://0.0.0.0:19198")
    print(f"  默认管理员: admin")
    print(f"  密码: admin123")
    print(f"{'='*60}\n")
    app.run(host='0.0.0.0', port=19198, debug=(env == 'development'))

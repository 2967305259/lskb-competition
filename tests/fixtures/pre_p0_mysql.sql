-- Pre-P0 schema generated from GitHub 8238f20 models; no application import/initialization.

CREATE TABLE roguelikes (
	id INTEGER NOT NULL AUTO_INCREMENT,
	`key` VARCHAR(30) NOT NULL,
	name VARCHAR(50) NOT NULL,
	is_active BOOL,
	max_per_team INTEGER,
	sort_order INTEGER,
	difficulty_min INTEGER,
	difficulty_max INTEGER,
	difficulty_step FLOAT,
	created_at DATETIME,
	PRIMARY KEY (id),
	UNIQUE (`key`)
)

;

CREATE TABLE rules (
	id INTEGER NOT NULL AUTO_INCREMENT,
	title VARCHAR(200) NOT NULL,
	content TEXT NOT NULL,
	roguelike_key VARCHAR(30),
	sort_order INTEGER,
	is_published BOOL,
	created_at DATETIME,
	updated_at DATETIME,
	PRIMARY KEY (id)
)

;

CREATE TABLE settings (
	id INTEGER NOT NULL AUTO_INCREMENT,
	registration_open BOOL,
	tournament_status VARCHAR(20),
	advance_count INTEGER,
	tournament_name VARCHAR(100),
	team_size INTEGER,
	allow_incomplete_teams BOOL,
	updated_at DATETIME,
	PRIMARY KEY (id)
)

;

CREATE TABLE tournaments (
	id INTEGER NOT NULL AUTO_INCREMENT,
	name VARCHAR(100) NOT NULL,
	description TEXT,
	season_number INTEGER NOT NULL,
	is_active BOOL,
	status VARCHAR(20),
	registration_start DATETIME,
	registration_end DATETIME,
	tournament_start DATETIME,
	tournament_end DATETIME,
	bonus_score INTEGER,
	created_at DATETIME,
	PRIMARY KEY (id)
)

;

CREATE TABLE users (
	id INTEGER NOT NULL AUTO_INCREMENT,
	username VARCHAR(20) NOT NULL,
	nickname VARCHAR(20) NOT NULL,
	password_hash VARCHAR(256) NOT NULL,
	`role` VARCHAR(20) NOT NULL,
	password_changed BOOL,
	avatar VARCHAR(500),
	status INTEGER,
	has_completed_onboarding BOOL,
	tutorial_step INTEGER,
	created_at DATETIME,
	updated_at DATETIME,
	PRIMARY KEY (id),
	UNIQUE (nickname)
)

;
CREATE UNIQUE INDEX ix_users_username ON users (username);

CREATE TABLE carousel_items (
	id INTEGER NOT NULL AUTO_INCREMENT,
	tournament_id INTEGER,
	media_type VARCHAR(10),
	file_path VARCHAR(500) NOT NULL,
	title VARCHAR(200),
	link_url VARCHAR(500),
	sort_order INTEGER,
	is_active BOOL,
	created_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(tournament_id) REFERENCES tournaments (id)
)

;

CREATE TABLE system_logs (
	id INTEGER NOT NULL AUTO_INCREMENT,
	user_id INTEGER,
	action VARCHAR(100) NOT NULL,
	detail TEXT,
	ip_address VARCHAR(50),
	created_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
)

;

CREATE TABLE teams (
	id INTEGER NOT NULL AUTO_INCREMENT,
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

;
CREATE INDEX ix_teams_team_name ON teams (team_name);

CREATE TABLE time_preferences (
	id INTEGER NOT NULL AUTO_INCREMENT,
	player_id INTEGER NOT NULL,
	day_of_week INTEGER NOT NULL,
	start_time VARCHAR(5) NOT NULL,
	end_time VARCHAR(5) NOT NULL,
	created_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(player_id) REFERENCES users (id)
)

;

CREATE TABLE invitations (
	id INTEGER NOT NULL AUTO_INCREMENT,
	team_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	status VARCHAR(20),
	created_at DATETIME,
	updated_at DATETIME,
	PRIMARY KEY (id),
	CONSTRAINT uq_team_invitation UNIQUE (team_id, user_id),
	FOREIGN KEY(team_id) REFERENCES teams (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
)

;

CREATE TABLE join_requests (
	id INTEGER NOT NULL AUTO_INCREMENT,
	team_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	status VARCHAR(20),
	created_at DATETIME,
	updated_at DATETIME,
	PRIMARY KEY (id),
	CONSTRAINT uq_team_join_request UNIQUE (team_id, user_id),
	FOREIGN KEY(team_id) REFERENCES teams (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
)

;

CREATE TABLE matches (
	id INTEGER NOT NULL AUTO_INCREMENT,
	tournament_id INTEGER,
	player_a_id INTEGER NOT NULL,
	player_b_id INTEGER NOT NULL,
	team_a_id INTEGER,
	team_b_id INTEGER,
	roguelike VARCHAR(20) NOT NULL,
	difficulty INTEGER,
	player_a_difficulty INTEGER,
	player_b_difficulty INTEGER,
	status VARCHAR(20),
	match_type VARCHAR(20),
	parent_id INTEGER,
	segment_order INTEGER,
	player_a_result VARCHAR(20),
	player_b_result VARCHAR(20),
	player_a_score INTEGER,
	player_b_score INTEGER,
	winner_id INTEGER,
	confirmed_a BOOL,
	confirmed_b BOOL,
	review_status VARCHAR(20),
	review_reason TEXT,
	scheduled_time DATETIME,
	match_note TEXT,
	created_at DATETIME,
	finished_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(tournament_id) REFERENCES tournaments (id),
	FOREIGN KEY(player_a_id) REFERENCES users (id),
	FOREIGN KEY(player_b_id) REFERENCES users (id),
	FOREIGN KEY(team_a_id) REFERENCES teams (id),
	FOREIGN KEY(team_b_id) REFERENCES teams (id),
	FOREIGN KEY(parent_id) REFERENCES matches (id),
	FOREIGN KEY(winner_id) REFERENCES users (id)
)

;

CREATE TABLE team_members (
	id INTEGER NOT NULL AUTO_INCREMENT,
	team_id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	position VARCHAR(20),
	roguelike VARCHAR(20),
	squad VARCHAR(50),
	operator VARCHAR(50),
	declaration VARCHAR(200),
	final_score INTEGER,
	joined_at DATETIME,
	updated_at DATETIME,
	PRIMARY KEY (id),
	CONSTRAINT uq_team_user UNIQUE (team_id, user_id),
	FOREIGN KEY(team_id) REFERENCES teams (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
)

;

CREATE TABLE score_logs (
	id INTEGER NOT NULL AUTO_INCREMENT,
	match_id INTEGER NOT NULL,
	modified_by INTEGER NOT NULL,
	old_result VARCHAR(20),
	new_result VARCHAR(20),
	reason TEXT,
	created_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(match_id) REFERENCES matches (id),
	FOREIGN KEY(modified_by) REFERENCES users (id)
)

;

-- CIA canonical SQLite schema
-- One DB per topic (lives in reports/<date>-cia-<topic>/cia.db)

CREATE TABLE IF NOT EXISTS topic_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS seeds (
    id INTEGER PRIMARY KEY,
    keyword TEXT NOT NULL,
    dimension TEXT,
    side TEXT,
    UNIQUE(keyword, dimension, side)
);

CREATE TABLE IF NOT EXISTS keywords_google (
    id INTEGER PRIMARY KEY,
    keyword TEXT NOT NULL,
    volume INTEGER,
    kd INTEGER,
    cpc_usd REAL,
    intent TEXT,
    traffic_potential INTEGER,
    parent_topic TEXT,
    country TEXT DEFAULT 'us',
    source_seed TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(keyword, country)
);
CREATE INDEX IF NOT EXISTS idx_kw_volume ON keywords_google(volume DESC);
CREATE INDEX IF NOT EXISTS idx_kw_cpc ON keywords_google(cpc_usd DESC);

CREATE TABLE IF NOT EXISTS appstore_serp (
    id INTEGER PRIMARY KEY,
    keyword TEXT NOT NULL,
    rank INTEGER,
    app_id TEXT,
    app_name TEXT,
    developer TEXT,
    rating REAL,
    review_count INTEGER,
    icon_url TEXT,
    country TEXT DEFAULT 'us',
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(keyword, app_id, country)
);
CREATE INDEX IF NOT EXISTS idx_serp_app ON appstore_serp(app_id);

CREATE TABLE IF NOT EXISTS appstore_keywords (
    id INTEGER PRIMARY KEY,
    app_id TEXT,
    app_name TEXT,
    keyword TEXT,
    rank INTEGER,
    search_volume INTEGER,
    country TEXT DEFAULT 'us',
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(app_id, keyword, country)
);
CREATE INDEX IF NOT EXISTS idx_appkw_volume ON appstore_keywords(search_volume DESC);

CREATE TABLE IF NOT EXISTS competitors_app (
    app_id TEXT PRIMARY KEY,
    name TEXT,
    subtitle TEXT,
    description TEXT,
    developer TEXT,
    rating REAL,
    review_count INTEGER,
    est_downloads_low INTEGER,
    est_downloads_high INTEGER,
    price TEXT,
    category TEXT,
    bundle_id TEXT,
    icon_url TEXT,
    url TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_reviews (
    id TEXT PRIMARY KEY,
    app_id TEXT,
    rating INTEGER,
    title TEXT,
    body TEXT,
    author TEXT,
    posted_at TIMESTAMP,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_review_app ON app_reviews(app_id);
CREATE INDEX IF NOT EXISTS idx_review_rating ON app_reviews(rating);

CREATE TABLE IF NOT EXISTS competitors_web (
    domain TEXT PRIMARY KEY,
    monthly_organic_traffic INTEGER,
    domain_rating INTEGER,
    backlinks_count INTEGER,
    refdomains_count INTEGER,
    organic_keywords_count INTEGER,
    paid_keywords_count INTEGER,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS competitor_organic_kw (
    id INTEGER PRIMARY KEY,
    domain TEXT,
    keyword TEXT,
    rank INTEGER,
    volume INTEGER,
    cpc_usd REAL,
    url TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(domain, keyword)
);

CREATE TABLE IF NOT EXISTS social_tiktok (
    id TEXT PRIMARY KEY,
    query TEXT,
    author TEXT,
    plays INTEGER,
    likes INTEGER,
    shares INTEGER,
    comments INTEGER,
    text TEXT,
    url TEXT,
    posted_at TIMESTAMP,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tt_likes ON social_tiktok(likes DESC);

CREATE TABLE IF NOT EXISTS social_reddit (
    id TEXT PRIMARY KEY,
    subreddit TEXT,
    query TEXT,
    title TEXT,
    body TEXT,
    score INTEGER,
    num_comments INTEGER,
    author TEXT,
    url TEXT,
    posted_at TIMESTAMP,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_rd_score ON social_reddit(score DESC);

CREATE TABLE IF NOT EXISTS social_youtube (
    id TEXT PRIMARY KEY,
    query TEXT,
    title TEXT,
    description TEXT,
    channel TEXT,
    views INTEGER,
    likes INTEGER,
    comments INTEGER,
    duration_seconds INTEGER,
    url TEXT,
    posted_at TIMESTAMP,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_yt_views ON social_youtube(views DESC);

CREATE TABLE IF NOT EXISTS ai_visibility (
    id INTEGER PRIMARY KEY,
    brand TEXT,
    platform TEXT,
    impressions INTEGER,
    mentions INTEGER,
    sov_pct REAL,
    citation_url TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Competitor cluster assignments (written by cluster-competitors command)
-- competitor_id = app_id (numeric) for App competitors, domain string for Web competitors
CREATE TABLE IF NOT EXISTS competitor_clusters (
    competitor_id TEXT PRIMARY KEY,  -- app_id or domain
    competitor_type TEXT DEFAULT 'app',  -- 'app' | 'web'
    domain TEXT,                     -- NULL for apps, domain string for web
    cluster_id INTEGER,
    cluster_label TEXT,
    top_keywords TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cc_cluster ON competitor_clusters(cluster_id);
CREATE INDEX IF NOT EXISTS idx_cc_type ON competitor_clusters(competitor_type);

-- Social data → cluster assignment (written by assign-to-clusters command)
CREATE TABLE IF NOT EXISTS social_cluster_map (
    source TEXT NOT NULL,       -- 'tiktok' | 'youtube' | 'reddit'
    item_id TEXT NOT NULL,
    cluster_id INTEGER,
    score REAL,
    assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source, item_id)
);

-- LLM-defined market track taxonomy (written by define-tracks command)
CREATE TABLE IF NOT EXISTS tracks (
    track_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,        -- Chinese name e.g. "AI接待类"
    name_en TEXT,              -- English name
    description TEXT,
    keywords TEXT,             -- comma-separated keyword patterns (lowercase)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Run log: who fetched what when, with cost tracking
CREATE TABLE IF NOT EXISTS fetch_log (
    id INTEGER PRIMARY KEY,
    source TEXT,
    op TEXT,
    args TEXT,
    rows INTEGER,
    cost_usd REAL,
    error TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    duration_ms INTEGER
);

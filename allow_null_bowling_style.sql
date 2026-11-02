ALTER TABLE batting_vs_bowling_matchups
    MODIFY COLUMN bowling_type ENUM('PACE', 'SPIN') NULL DEFAULT NULL,
    MODIFY COLUMN bowling_technique VARCHAR(40) NULL DEFAULT NULL;

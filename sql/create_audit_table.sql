USE [Bolthouse_Ag_AI];
GO

IF OBJECT_ID(N'dbo.MCP_Query_Audit_AG_Railway', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.MCP_Query_Audit_AG_Railway
    (
        AuditID        bigint IDENTITY(1,1) NOT NULL PRIMARY KEY,
        RequestID      uniqueidentifier NOT NULL,
        Operation      nvarchar(50) NOT NULL,
        QueryText      nvarchar(max) NOT NULL,
        Status         nvarchar(20) NOT NULL,
        RowsReturned   bigint NULL,
        ErrorMessage   nvarchar(max) NULL,
        CreatedUTC     datetime2(3) NOT NULL
            CONSTRAINT DF_MCP_Query_Audit_AG_Railway_CreatedUTC
            DEFAULT SYSUTCDATETIME()
    );
END;
GO

GRANT INSERT ON dbo.MCP_Query_Audit_AG_Railway TO [sqlprd_ag_ai];
GO

-- After creating the table, set this Railway variable:
-- AUDIT_TABLE=dbo.MCP_Query_Audit_AG_Railway

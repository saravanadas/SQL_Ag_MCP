EXECUTE AS LOGIN = 'sqlprd_ag_ai';
GO

SELECT
    ORIGINAL_LOGIN() AS OriginalLogin,
    SUSER_SNAME() AS EffectiveLogin,
    DB_NAME() AS CurrentDatabase;
GO

USE [Bolthouse_Ag_AI];
GO

SELECT TOP (10)
    s.name AS SchemaName,
    o.name AS ObjectName,
    o.type_desc AS ObjectType
FROM sys.objects AS o
INNER JOIN sys.schemas AS s
    ON s.schema_id = o.schema_id
WHERE o.type IN ('U', 'V')
ORDER BY s.name, o.name;
GO

REVERT;
GO

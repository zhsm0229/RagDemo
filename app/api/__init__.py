"""API 层：薄控制器（路由/DTO/依赖注入），业务逻辑一律委托 services。

依赖注入链（后端是唯一权威校验，standards §5.5）：
get_current_user（Cookie→decode→查库 is_active，401/2003）
→ require_password_changed（首登未改密拒绝业务接口，403/2004）
→ require_admin（角色校验，403/2004）。
"""

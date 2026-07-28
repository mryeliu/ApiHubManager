# ApiHubManager
API管理系统
## 一、Docker Compose 部署（推荐，自带 MySQL + Redis）

##  安装好 docker  compose   (步骤省略)   将API_Hub_MANAGER 上传到服务器 
```bash
cd api_manager
docker compose up -d --build      # --build 必须带（依赖变了要重建镜像）
docker compose ps                 # 三个服务都 healthy 即成功
```



### 4. 验证
浏览器打开 `http://<服务器IP>:8000/admin`，首次进入初始化管理员口令页。
服务器防火墙放行 8000 端口：`sudo firewall-cmd --add-port=8000/tcp --permanent && sudo firewall-cmd --reload`
docker compose logs -f app        # 看程序日志




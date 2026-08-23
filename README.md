# qBittorrent - FitGirl Repacks Search Plugin

Plugin de búsqueda para el cliente BitTorrent qBittorrent que permite indexar y consultar las publicaciones del sitio fitgirl-repacks.site.

## Características

- Búsqueda concurrente mediante hilos para optimizar tiempos de respuesta.
- Extracción de enlaces magnet y archivos torrent.
- Detección de tamaño de descarga y fechas de publicación.
- Filtrado automático de entradas no relacionadas con lanzamientos de juegos.

## Instalación

### Método 1: Instalación directa mediante URL

1. En qBittorrent, acceda a la pestaña **Buscador** (*Search*).
2. Haga clic en **Extensiones de búsqueda...** (*Search plugins...*).
3. Seleccione **Instalar uno nuevo** (*Install a new one*).
4. Elija la opción **Enlace web** (*Web link*) e introduzca la siguiente URL:
   ```text
   https://raw.githubusercontent.com/afalvarezsite/qbtFitGirl-search/main/fitgirl_repacks.py
   ```
5. Confirme para finalizar la instalación.

### Método 2: Instalación manual

1. Descargue el archivo `fitgirl_repacks.py` de este repositorio.
2. En qBittorrent, diríjase a **Buscador** > **Extensiones de búsqueda...** > **Instalar uno nuevo**.
3. Seleccione **Archivo local** (*Local file*) y cargue el archivo descargado.

## Requisitos

- qBittorrent v4.x o superior.
- Entorno de ejecución Python 3 configurado en el sistema.

## Licencia

Este proyecto está distribuido bajo los términos de la licencia GNU General Public License v3.0 (GPLv3). Consulte el archivo LICENSE para más información.

## Descargo de responsabilidad

Este software se proporciona únicamente con fines informativos y de interoperabilidad técnica. El proyecto no aloja contenido protegido por derechos de autor ni mantiene relación directa con el sitio indexado.

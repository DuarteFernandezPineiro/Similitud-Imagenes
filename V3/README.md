# V3 — agrupador de imágenes similares con móviles MTP

V3 analiza carpetas del ordenador o la galería pública de un móvil conectado
por USB. Los vídeos se omiten del análisis y se admiten los formatos de imagen
habituales, incluidos HEIC/HEIF. No se modifica ningún original al analizar.

La similitud es **perceptual**, no por nombre ni tamaño: una misma foto
recomprimida, redimensionada o con leves cambios normalmente conserva un hash
parecido. El valor de 100% significa que el hash perceptual es idéntico; no es
una comprobación de que los bytes del archivo sean iguales.

## Instalación

En PowerShell, desde esta carpeta:

```powershell
python -m pip install -r requirements.txt
```

## Uso

### Interfaz gráfica

Tras instalar las dependencias, inicia la aplicación con:

```powershell
python interfaz.py
```

La pantalla guía el proceso en tres pasos: elegir una carpeta (el análisis
empieza automáticamente), revisar los grupos y borrar solo las fotos elegidas.
Permite escoger el umbral y cuántas imágenes se muestran por fila. Un clic
selecciona o deselecciona una imagen para borrado y una pulsación mantenida la
abre a pantalla completa. La ventana ampliada se cierra inmediatamente con
`Esc`.

Mientras se realiza el primer análisis aparecen grupos utilizables: pueden
revisarse, seleccionarse y borrarse sin esperar al final. Las actualizaciones
se aplican al avanzar al siguiente grupo o al pulsar `Ver grupos`, para no
interrumpir la revisión actual. El borrado siempre requiere una confirmación
explícita y elimina los archivos de forma permanente.

### Móviles conectados por cable (Windows)

Pulsa **Elegir dispositivo** si las fotos están en un teléfono conectado por
USB. El móvil debe estar desbloqueado y configurado en modo **Transferir
archivos**. V3 detecta los dispositivos MTP que aparecen en **Este equipo** y
recorre las ubicaciones públicas de multimedia: `DCIM`, `Pictures`, `Movies`,
`Download`, `Documents`, carpetas de mensajería y `Android\\media`.

Las zonas de sistema (`Android\\data`, `Android\\obb`) y las carpetas ocultas
se omiten porque Android/MTP suele impedir su lectura o bloquear el recorrido.
Solo se copian temporalmente imágenes compatibles a una caché local en
`%LOCALAPPDATA%\\SimilitudImagenes\\moviles`; los vídeos no se copian. Después
se analizan y revisan como las imágenes de una carpeta normal. La caché se
reutiliza entre ejecuciones. Un manifiesto persistente permite abrir de forma
inmediata una galería ya preparada, sin volver a recorrer miles de entradas
MTP ni consultar cada copia local. El botón **Actualizar** fuerza un recorrido
completo para incorporar fotos nuevas, modificadas o eliminadas fuera de V3.

Durante ese recorrido se muestra la carpeta actual y el número de imágenes ya
localizadas. V3 compara el tamaño remoto con la copia local y transfiere solo
los cambios; el análisis recibe directamente el mismo inventario, por lo que
no vuelve a recorrer la caché una segunda vez.

El botón de eliminar solo actúa sobre los originales seleccionados después de
una confirmación explícita. La interfaz conserva las miniaturas locales hasta
que Windows confirma que cada original ya no existe en el móvil. El teléfono
tiene que permanecer encendido, desbloqueado y conectado durante la importación
y durante un borrado.

Al borrar varias imágenes, V3 solicita una única confirmación para la selección
completa. Después entrega todos los elementos juntos a `IFileOperation` con
`FOF_NOCONFIRMATION`, de modo que Windows realiza una sola operación silenciosa
y no muestra un aviso por archivo. El resultado se comprueba en el dispositivo
antes de retirar las miniaturas. Para proveedores MTP antiguos que rechacen esa
API existe un fallback restringido a los nombres ya confirmados por el usuario.

### Línea de comandos

```powershell
python agrupar_imagenes.py "C:\\Fotos" --similitud 90 --salida "C:\\Fotos\\grupos_similares.json"
```

Los únicos valores permitidos para `--similitud` son `80`, `90`, `95` y `100`.
Si se omite, se usa `90`.

Para limitar el uso de CPU, por ejemplo a cuatro procesos:

```powershell
python agrupar_imagenes.py "C:\\Fotos" --similitud 95 --procesos 4
```

## Ejecuciones posteriores

En la primera ejecución se crea automáticamente el archivo oculto
`.similitud_imagenes.sqlite` dentro de la carpeta analizada. Contiene la ruta,
el tamaño, la fecha de modificación y el pHash de cada imagen.

En las siguientes ejecuciones el programa recorre las rutas para detectar
cambios, pero solo vuelve a calcular el hash de las imágenes nuevas o cuyos
tamaño o fecha de modificación hayan cambiado. También elimina de la caché las
entradas de los archivos que ya no existen. El JSON indicará cuántos hashes se
reutilizaron y cuántos se calcularon en esa ejecución.

Para imágenes **nuevas**, SQLite busca sus candidatas en el índice persistente
y actualiza únicamente los grupos a los que se conecten. No vuelve a comparar
ni a reconstruir los grupos ya existentes. Si se modifica o borra una imagen,
o se cambia el porcentaje de similitud, se reconstruyen todos los grupos: es
necesario porque un grupo podría dividirse.

Se puede guardar la caché en otra ubicación con `--cache`:

```powershell
python agrupar_imagenes.py "C:\\Fotos" --similitud 90 --cache "D:\\datos\\fotos-cache.sqlite"
```

## Rendimiento

Primero se calcula un pHash de 64 bits de cada imagen en paralelo. Después se
usa un índice por fragmentos del hash para obtener únicamente candidatas y se
verifica la distancia exacta antes de unirlas. Así se evita comparar cada foto
contra todas las demás, algo inviable con 100.000 imágenes. Las imágenes que
conectan entre sí por encima del umbral se incluyen en el mismo grupo.

Las miniaturas se decodifican en paralelo y se conservan en memoria. La vista
general se construye por bloques y muestra como máximo doce vistas previas por
grupo, manteniendo accesibles todas las imágenes al abrirlo sin congelar la
interfaz cuando existen cientos de grupos.

## Pruebas

Las pruebas automáticas comprueban el índice contra una comparación exhaustiva
en todos los umbrales, el manifiesto móvil y un borrado nativo real de archivos
temporales sin avisos del Shell:

```powershell
python -m unittest discover -s tests -v
```

Con un teléfono conectado se puede medir el refresco completo y la reapertura
desde caché sin borrar contenido:

```powershell
python tests\\manual_mtp_smoke.py
```

La prueba de borrado crea una imagen azul con un nombre UUID dentro de
`Pictures`, la elimina mediante el mismo lote nativo y comprueba que el móvil
confirma su desaparición. No utiliza ninguna foto existente:

```powershell
python tests\\manual_mtp_delete_smoke.py
python tests\\manual_mtp_delete_smoke.py --count 10
```

"""Servicio de envio de correos mediante Amazon Simple Email Service (SES).

Envía:
- Correos de bienvenida con contrasena temporal
- Codigos de recuperacion de contrasena
- Notificaciones de solicitudes de informes aprobadas/rechazadas
"""

from __future__ import annotations

import os
from typing import Mapping, Optional

import boto3
from botocore.exceptions import ClientError


class SESServices:
    """Cliente SES para envio de correos del sistema."""

    _instance: Optional["SESServices"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        region = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
        endpoint = os.getenv("SES_ENDPOINT_URL")

        config: dict = {"region_name": region}
        if endpoint:
            config["endpoint_url"] = endpoint

        self._client = boto3.client("ses", **config)

        self.sender_email = os.getenv(
            "SES_SENDER_ADDRESS",
            os.getenv("SES_SENDER_EMAIL", os.getenv("EMAIL_SENDER", "")),
        ).strip()
        self.sender_name = os.getenv(
            "SES_SENDER_NAME",
            "EmotionLab - Sistema de Evaluacion",
        ).strip()

        self.frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173").strip()
        self._initialized = True

    @property
    def source_address(self) -> str:
        return f"{self.sender_name} <{self.sender_email}>"

    def _send(
        self,
        to_addresses: list[str],
        subject: str,
        body_html: str,
        body_text: Optional[str] = None,
        reply_to: Optional[list[str]] = None,
    ) -> bool:
        if not to_addresses:
            return False

        if body_text is None:
            body_text = "Este mensaje contiene contenido HTML. Usa un cliente que soporte HTML."

        destination = {
            "ToAddresses": to_addresses,
        }
        message = {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Html": {"Data": body_html, "Charset": "UTF-8"},
                "Text": {"Data": body_text, "Charset": "UTF-8"},
            },
        }
        try:
            self._client.send_email(
                Source=self.source_address,
                Destination=destination,
                Message=message,
                ReplyToAddresses=reply_to or [self.sender_email],
            )
            return True
        except ClientError as err:
            print(f"[SES] Error enviando correo (tipo={type(err).__name__})", flush=True)
            return False
        except Exception as err:
            print(f"[SES] Error inesperado (tipo={type(err).__name__})", flush=True)
            return False

    # ------------------------------------------------------------------
    # Templates de correos
    # ------------------------------------------------------------------
    @staticmethod
    def _html_wrapper(title: str, body: str) -> str:
        return f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #17315E;">
            <div style="border-bottom: 2px solid #17315E; padding-bottom: 12px; margin-bottom: 20px;">
                <h2 style="margin: 0; color: #17315E;">EmotionLab</h2>
            </div>
            <div style="font-size: 15px; line-height: 1.6;">
                {body}
            </div>
            <div style="margin-top: 30px; padding-top: 15px; border-top: 1px solid #E5E7EB; font-size: 12px; color: #6B7280;">
                <p>Este correo fue generado automaticamente por el sistema EmotionLab.</p>
                <p>Si no reconoces esta accion, contacta al administrador.</p>
            </div>
        </div>
        """

    def send_welcome_with_temp_password(
        self,
        to_email: str,
        nombre: str,
        temporal_password: str,
        rol: str,
    ) -> bool:
        body = f"""
            <p>Hola <strong>{nombre}</strong>,</p>
            <p>Tu cuenta en EmotionLab ha sido creada exitosamente.</p>
            <ul>
                <li><strong>Usuario (email):</strong> {to_email}</li>
                <li><strong>Rol:</strong> {rol}</li>
                <li><strong>Contrasena temporal:</strong>
                    <span style="font-family: monospace; background: #F3F4F6; padding: 4px 8px; border-radius: 4px; font-size: 18px; letter-spacing: 2px;">{temporal_password}</span>
                </li>
            </ul>
            <p style="background: #FFF4E5; border-left: 4px solid #B45309; padding: 12px 16px; margin: 20px 0;">
                <strong>Importante:</strong> Al iniciar sesion por primera vez, el sistema te pedira cambiar la contrasena. La contrasena temporal vence en 7 dias.
            </p>
            <p>Puedes acceder aqui: <a href="{self.frontend_url}">{self.frontend_url}</a></p>
            <p>Si tienes dudas, contacta al administrador del sistema.</p>
        """
        subject = "Bienvenido(a) a EmotionLab - Tu cuenta ha sido creada"
        return self._send([to_email], subject, self._html_wrapper(subject, body))

    def send_password_reset_code(
        self,
        to_email: str,
        code: str,
        nombre: Optional[str] = None,
    ) -> bool:
        greeting = f"Hola <strong>{nombre}</strong>," if nombre else "Hola,"
        body = f"""
            <p>{greeting}</p>
            <p>Hemos recibido una solicitud para restablecer tu contrasena de EmotionLab.</p>
            <p style="text-align: center; margin: 30px 0;">
                Tu codigo de verificacion es:<br>
                <span style="font-family: monospace; background: #17315E; color: white; padding: 10px 20px; border-radius: 6px; font-size: 28px; letter-spacing: 8px;">{code}</span>
            </p>
            <p>Ingresa este codigo en la pantalla de recuperacion para crear una nueva contrasena.</p>
            <p style="color: #B45309;">Este codigo vence en 15 minutos. Si no lo solicitaste, puedes ignorar este correo.</p>
        """
        subject = "Codigo de recuperacion de contrasena - EmotionLab"
        return self._send([to_email], subject, self._html_wrapper(subject, body))

    def send_report_notification(
        self,
        to_email: str,
        nombre: str,
        aprobado: bool,
        session_id: str,
        rejection_reason: Optional[str] = None,
    ) -> bool:
        estado = "Aprobado" if aprobado else "Rechazado"
        color = "#059669" if aprobado else "#DC2626"
        motivo = ""
        if not aprobado and rejection_reason:
            motivo = f'<p><strong>Motivo del rechazo:</strong> {rejection_reason}</p>'
        body = f"""
            <p>Hola <strong>{nombre}</strong>,</p>
            <p>Tu solicitud de informe para la sesion <strong>{session_id}</strong> ha sido:</p>
            <p style="text-align: center; margin: 25px 0;">
                <span style="display: inline-block; background: {color}; color: white; padding: 10px 24px; border-radius: 6px; font-size: 18px; font-weight: bold;">
                    {estado.upper()}
                </span>
            </p>
            {motivo}
            <p>Accede al sistema para mas detalles: <a href="{self.frontend_url}">{self.frontend_url}</a></p>
        """
        subject = f"Solicitud de informe {estado} - EmotionLab"
        return self._send([to_email], subject, self._html_wrapper(subject, body))

    def send_password_changed_confirmation(
        self,
        to_email: str,
        nombre: Optional[str] = None,
    ) -> bool:
        greeting = f"Hola <strong>{nombre}</strong>," if nombre else "Hola,"
        body = f"""
            <p>{greeting}</p>
            <p>Te confirmamos que tu contrasena de EmotionLab fue actualizada exitosamente.</p>
            <p style="background: #FEF2F2; border-left: 4px solid #DC2626; padding: 12px 16px; margin: 20px 0;">
                <strong>Seguridad:</strong> Si no realizaste este cambio, contacta inmediatamente al administrador y cambia tu contrasena.
            </p>
        """
        subject = "Tu contrasena ha sido actualizada - EmotionLab"
        return self._send([to_email], subject, self._html_wrapper(subject, body))


# Alias para compatibilidad con los nombres usados en el DI factory (app.py)
# y constructores de servicios de dominio (auth_service / report_request_service).
SESService = SESServices


"""Tests for MCP initialization handshake."""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from murl.cli import perform_initialization_handshake
from murl import __version__


def test_initialization_handshake_success():
    """Test successful initialization handshake."""
    # Mock the POST request for initialize
    mock_init_response = Mock()
    mock_init_response.status_code = 202
    
    # Mock the POST request for initialized notification
    mock_notif_response = Mock()
    mock_notif_response.status_code = 202
    
    # Mock SSE stream lines iterator
    init_response_data = {
        "jsonrpc": "2.0",
        "id": 0,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {
                "name": "test-server",
                "version": "1.0.0"
            }
        }
    }
    
    lines_iter = iter([
        "event: message",
        f"data: {json.dumps(init_response_data)}",
    ])
    
    with patch('murl.cli.requests.post') as mock_post:
        mock_post.side_effect = [mock_init_response, mock_notif_response]
        
        result = perform_initialization_handshake(
            "http://localhost:3000/session/123",
            lines_iter,
            verbose=False
        )
        
        assert result is True
        assert mock_post.call_count == 2
        
        # Verify initialize request
        init_call = mock_post.call_args_list[0]
        init_request = init_call[1]['json']
        assert init_request['method'] == 'initialize'
        assert init_request['params']['protocolVersion'] == '2024-11-05'
        assert init_request['params']['clientInfo']['name'] == 'murl'
        assert init_request['params']['clientInfo']['version'] == __version__
        
        # Verify initialized notification
        notif_call = mock_post.call_args_list[1]
        notif_request = notif_call[1]['json']
        assert notif_request['method'] == 'notifications/initialized'


def test_initialization_handshake_init_request_fails():
    """Test initialization handshake when initialize request fails."""
    mock_init_response = Mock()
    mock_init_response.status_code = 500
    
    lines_iter = iter([])
    
    with patch('murl.cli.requests.post') as mock_post:
        mock_post.return_value = mock_init_response
        
        result = perform_initialization_handshake(
            "http://localhost:3000/session/123",
            lines_iter,
            verbose=False
        )
        
        assert result is False


def test_initialization_handshake_no_response():
    """Test initialization handshake when no response received."""
    mock_init_response = Mock()
    mock_init_response.status_code = 202
    
    # Empty lines iterator - timeout
    lines_iter = iter([])
    
    with patch('murl.cli.requests.post') as mock_post:
        mock_post.return_value = mock_init_response
        
        with patch('murl.cli.time.time') as mock_time:
            # Simulate timeout
            mock_time.side_effect = [0, 0, 0, 20]  # Force timeout
            
            result = perform_initialization_handshake(
                "http://localhost:3000/session/123",
                lines_iter,
                verbose=False
            )
            
            assert result is False


def test_initialization_handshake_error_response():
    """Test initialization handshake when server returns error."""
    mock_init_response = Mock()
    mock_init_response.status_code = 202
    
    # Mock error response from server
    error_response_data = {
        "jsonrpc": "2.0",
        "id": 0,
        "error": {
            "code": -32602,
            "message": "Invalid parameters"
        }
    }
    
    lines_iter = iter([
        f"data: {json.dumps(error_response_data)}",
    ])
    
    with patch('murl.cli.requests.post') as mock_post:
        mock_post.return_value = mock_init_response
        
        result = perform_initialization_handshake(
            "http://localhost:3000/session/123",
            lines_iter,
            verbose=False
        )
        
        assert result is False


def test_initialization_handshake_notification_fails():
    """Test initialization handshake when initialized notification fails."""
    mock_init_response = Mock()
    mock_init_response.status_code = 202
    
    mock_notif_response = Mock()
    mock_notif_response.status_code = 500
    
    init_response_data = {
        "jsonrpc": "2.0",
        "id": 0,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {
                "name": "test-server",
                "version": "1.0.0"
            }
        }
    }
    
    lines_iter = iter([
        f"data: {json.dumps(init_response_data)}",
    ])
    
    with patch('murl.cli.requests.post') as mock_post:
        mock_post.side_effect = [mock_init_response, mock_notif_response]
        
        result = perform_initialization_handshake(
            "http://localhost:3000/session/123",
            lines_iter,
            verbose=False
        )
        
        assert result is False

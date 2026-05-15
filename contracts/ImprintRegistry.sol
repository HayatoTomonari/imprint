// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title ImprintRegistry
 * @notice 写真のSHA-256ハッシュをオンチェーンに記録し、真正性を証明するレジストリ。
 *         一度記録されたハッシュは変更不可。ブロックタイムスタンプが証跡となる。
 */
contract ImprintRegistry {
    struct Record {
        address registrar;  // 登録者アドレス
        uint256 timestamp;  // ブロックタイムスタンプ（Unix秒）
        string  metadata;   // JSON文字列（ファイル名・スコア等）
    }

    // imageHash(bytes32) → Record
    mapping(bytes32 => Record) private _records;

    event HashRegistered(
        bytes32 indexed imageHash,
        address indexed registrar,
        uint256 timestamp
    );

    /**
     * @notice ハッシュを登録する。既登録の場合はリバート。
     * @param imageHash SHA-256ハッシュ値（32バイト）
     * @param metadata  任意のJSON文字列（ファイル名・真正性スコア等）
     */
    function register(bytes32 imageHash, string calldata metadata) external {
        require(_records[imageHash].timestamp == 0, "ImprintRegistry: already registered");
        _records[imageHash] = Record(msg.sender, block.timestamp, metadata);
        emit HashRegistered(imageHash, msg.sender, block.timestamp);
    }

    /**
     * @notice ハッシュの登録状態を照会する（読み取り専用・無料）。
     */
    function verify(bytes32 imageHash)
        external
        view
        returns (
            bool    exists,
            address registrar,
            uint256 timestamp,
            string  memory metadata
        )
    {
        Record storage r = _records[imageHash];
        exists    = r.timestamp != 0;
        registrar = r.registrar;
        timestamp = r.timestamp;
        metadata  = r.metadata;
    }
}
